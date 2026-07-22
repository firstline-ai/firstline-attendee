import base64
import json
import logging
import os
import random
import signal
import time
import uuid

import docker
import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, models, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Organization
from bots.models import (
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    Calendar,
    CalendarStates,
    Recording,
    RecordingDeliveryStates,
    TranscriptionTypes,
    Utterance,
    ZoomOAuthConnection,
    ZoomOAuthConnectionStates,
)
from bots.tasks.autopay_charge_task import enqueue_autopay_charge_task
from bots.tasks.launch_scheduled_bot_task import launch_scheduled_bot
from bots.tasks.process_utterance_task import TRANSCRIPTION_CLAIM_STALE_SECONDS, process_utterance
from bots.tasks.recording_delivery_task import (
    MAX_RECORDING_DELIVERY_RETRIES,
    RECORDING_DELIVERY_ACTIVE_SECONDS,
    cleanup_ready_recording_spool,
    enqueue_recording_delivery,
    recover_recording_from_spool,
)
from bots.tasks.refresh_zoom_oauth_connection_task import enqueue_refresh_zoom_oauth_connection_task
from bots.tasks.sync_calendar_task import enqueue_sync_calendar_task
from bots.tasks.sync_zoom_oauth_connection_task import enqueue_sync_zoom_oauth_connection_task

log = logging.getLogger(__name__)

CALENDAR_SYNC_THRESHOLD_HOURS = 24  # The longest a calendar can go without having been synced
RECORDING_DELIVERY_REENQUEUE_SECONDS = int(os.getenv("RECORDING_DELIVERY_REENQUEUE_SECONDS", 300))
RECORDING_ORPHAN_RECOVERY_GRACE_SECONDS = int(os.getenv("RECORDING_ORPHAN_RECOVERY_GRACE_SECONDS", 120))


class Command(BaseCommand):
    help = "Runs celery tasks for scheduled bots."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=60,
            help="Polling interval in seconds (default: 60)",
        )

    # Graceful shutdown flags
    _keep_running = True
    _redis_client = None

    def _graceful_exit(self, signum, frame):
        log.info("Received %s, shutting down after current cycle", signum)
        self._keep_running = False

    def _get_redis_client(self):
        """Get or create a Redis client connection."""
        if self._redis_client is None:
            self._redis_client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
        return self._redis_client

    def _log_celery_queue_size(self, queue_name):
        """Log the size of the default Celery queue."""
        try:
            queue_size = self._get_redis_client().llen(queue_name)
            log.info("Celery queue %s size: %d", queue_name, queue_size)
        except Exception:
            log.exception("Failed to get Celery queue %s size", queue_name)
            self._redis_client = None  # Reset connection on failure

    def _log_celery_queue_sizes(self):
        try:
            # Get all the celery queue names from the CELERY_TASK_ROUTES setting
            queue_names = list({"celery"} | {route.get("queue", "celery") for route in settings.CELERY_TASK_ROUTES.values()})
            for queue_name in queue_names:
                self._log_celery_queue_size(queue_name)
        except Exception:
            log.exception("Failed to get Celery queue sizes, skipping")

    def handle(self, *args, **opts):
        # Trap SIGINT / SIGTERM so Kubernetes or Heroku can stop the container cleanly
        signal.signal(signal.SIGINT, self._graceful_exit)
        signal.signal(signal.SIGTERM, self._graceful_exit)

        interval = opts["interval"]
        log.info("Scheduler daemon started, polling every %s seconds", interval)

        while self._keep_running:
            began = time.monotonic()
            try:
                self._log_celery_queue_sizes()
                self._run_scheduled_bots()
                self._run_periodic_calendar_syncs()
                self._run_periodic_zoom_oauth_connection_syncs()
                self._run_periodic_zoom_oauth_connection_token_refreshs()
                self._run_autopay_tasks()
                self._run_stale_transcription_claims()
                self._run_pending_recording_deliveries()
            except Exception:
                log.exception("Scheduler cycle failed")
            finally:
                # Close stale connections so the loop never inherits a dead socket
                connection.close()

            # Sleep the *remainder* of the interval, even if work took time T
            elapsed = time.monotonic() - began
            remaining_sleep = max(0, interval - elapsed)

            # Break sleep into smaller chunks to allow for more responsive shutdown
            sleep_chunk = 1  # Sleep 1 second at a time
            while remaining_sleep > 0 and self._keep_running:
                chunk_sleep = min(sleep_chunk, remaining_sleep)
                time.sleep(chunk_sleep)
                remaining_sleep -= chunk_sleep

            # If we took longer than the interval, we should log a warning
            if elapsed > interval:
                log.warning(f"Scheduler cycle took {elapsed}s, which is longer than the interval of {interval}s")

        log.info("Scheduler daemon exited")

    def _run_periodic_calendar_syncs(self):
        """
        Run periodic calendar syncs.
        Launch sync tasks for calendars that haven't had a sync task enqueued in the last 24 hours.
        """
        now = timezone.now()
        cutoff_time = now - timezone.timedelta(hours=CALENDAR_SYNC_THRESHOLD_HOURS)

        # Find connected calendars that haven't had a sync task enqueued in the last 24 hours
        calendars = Calendar.objects.filter(
            state=CalendarStates.CONNECTED,
        ).filter(Q(sync_task_enqueued_at__isnull=True) | Q(sync_task_enqueued_at__lte=cutoff_time) | Q(sync_task_requested_at__isnull=False))

        for calendar in calendars:
            last_enqueued = calendar.sync_task_enqueued_at.isoformat() if calendar.sync_task_enqueued_at else "never"
            log.info("Launching calendar sync for calendar %s (last enqueued: %s)", calendar.object_id, last_enqueued)
            enqueue_sync_calendar_task(calendar)

        log.info("Launched %d calendar sync tasks", len(calendars))

    def _run_periodic_zoom_oauth_connection_token_refreshs(self):
        """
        Run periodic zoom oauth connection token refreshs.
        Launch token refresh tasks for zoom oauth connections that haven't had a token refresh task enqueued in the last 30 days.
        """
        now = timezone.now()
        cutoff_time = now - timezone.timedelta(days=30)

        zoom_oauth_connections = ZoomOAuthConnection.objects.filter(
            state=ZoomOAuthConnectionStates.CONNECTED,
        ).filter(Q(token_refresh_task_enqueued_at__isnull=True) | Q(token_refresh_task_enqueued_at__lte=cutoff_time) | Q(token_refresh_task_requested_at__isnull=False))

        for zoom_oauth_connection in zoom_oauth_connections:
            last_enqueued = zoom_oauth_connection.token_refresh_task_enqueued_at.isoformat() if zoom_oauth_connection.token_refresh_task_enqueued_at else "never"
            log.info("Launching zoom oauth connection token refresh for zoom oauth connection %s (last enqueued: %s)", zoom_oauth_connection.object_id, last_enqueued)
            enqueue_refresh_zoom_oauth_connection_task(zoom_oauth_connection)

        log.info("Launched %d zoom oauth connection token refresh tasks", len(zoom_oauth_connections))

    def _run_periodic_zoom_oauth_connection_syncs(self):
        """
        Run periodic zoom oauth connection syncs.
        Launch sync tasks for zoom oauth connections that haven't had a sync task enqueued in the last 7 days.
        """
        now = timezone.now()
        cutoff_time = now - timezone.timedelta(days=7)

        # Find connected zoom oauth connections that haven't had a sync task enqueued in the last 7 days
        zoom_oauth_connections = ZoomOAuthConnection.objects.filter(
            state=ZoomOAuthConnectionStates.CONNECTED,
            is_local_recording_token_supported=True,
        ).filter(Q(sync_task_enqueued_at__isnull=True) | Q(sync_task_enqueued_at__lte=cutoff_time) | Q(sync_task_requested_at__isnull=False))

        for zoom_oauth_connection in zoom_oauth_connections:
            last_enqueued = zoom_oauth_connection.sync_task_enqueued_at.isoformat() if zoom_oauth_connection.sync_task_enqueued_at else "never"
            log.info("Launching zoom oauth connection sync for zoom oauth connection %s (last enqueued: %s)", zoom_oauth_connection.object_id, last_enqueued)
            enqueue_sync_zoom_oauth_connection_task(zoom_oauth_connection)

        log.info("Launched %d zoom oauth connection sync tasks", len(zoom_oauth_connections))

    def _run_scheduled_bots_with_jitter(self):
        jitter_start_seconds = int(os.getenv("SCHEDULED_BOT_JITTER_START_SECONDS", 300))
        jitter_end_seconds = int(os.getenv("SCHEDULED_BOT_JITTER_END_SECONDS", 600))

        pending_scheduled_bot_task_args = self._get_args_for_pending_launch_scheduled_bot_tasks()
        log.info(f"Found {len(pending_scheduled_bot_task_args)} pending launch scheduled bot tasks")

        join_at_upper_threshold = timezone.now() + timezone.timedelta(seconds=jitter_end_seconds)
        # If we miss a scheduled bot by more than 5 minutes, don't bother launching it, it's a failure and it'll be cleaned up
        # by the clean_up_bots_with_heartbeat_timeout_or_that_never_launched command
        join_at_lower_threshold = timezone.now() - timezone.timedelta(minutes=5)

        join_at_jitter_threshold = timezone.now() + timezone.timedelta(seconds=jitter_start_seconds)

        with transaction.atomic():
            bots_to_launch = Bot.objects.filter(state=BotStates.SCHEDULED, join_at__lte=join_at_upper_threshold, join_at__gte=join_at_lower_threshold).select_for_update(skip_locked=True)

            num_bots_launched = 0
            for bot in bots_to_launch:
                if (bot.id, bot.join_at.isoformat()) in pending_scheduled_bot_task_args:
                    # The bot is already being launched, so we can skip it
                    continue

                if bot.join_at > join_at_jitter_threshold:
                    # The bot is above the jitter threshold, so we launch it with a random delay of up to bot.join_at - join_at_jitter_threshold seconds
                    random_delay = random.randint(0, int((bot.join_at - join_at_jitter_threshold).total_seconds()))
                    log.info(f"Launching scheduled bot {bot.id} ({bot.object_id}) with join_at {bot.join_at.isoformat()} and random delay {random_delay} seconds")
                    launch_scheduled_bot.apply_async(args=[bot.id, bot.join_at.isoformat()], countdown=random_delay)
                else:
                    # The bot is below the jitter threshold, so we need to launch immediately
                    log.info(f"Launching scheduled bot {bot.id} ({bot.object_id}) with join_at {bot.join_at.isoformat()}")
                    launch_scheduled_bot.delay(bot.id, bot.join_at.isoformat())

                num_bots_launched += 1

            log.info("Launched %s bots", num_bots_launched)

    def _get_args_for_pending_launch_scheduled_bot_tasks(self):
        try:
            scheduled_bot_task_args = set()
            for delivery_tag, raw in self._get_redis_client().hscan_iter("unacked", match="*"):
                # Filter for this string being in the raw message: bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot
                if b"bots.tasks.launch_scheduled_bot_task.launch_scheduled_bot" not in raw:
                    continue
                # Parse the raw message as JSON. First argument is bot id, second argument is join_at
                message = json.loads(raw)
                body = json.loads(base64.b64decode(message[0]["body"]))
                scheduled_bot_task_args.add((body[0][0], body[0][1]))

            return scheduled_bot_task_args
        except Exception:
            log.exception("Failed to get args for pending launch scheduled bot tasks")
            return set()

    # -----------------------------------------------------------
    def _run_scheduled_bots(self):
        if os.getenv("SCHEDULED_BOT_JITTER_START_SECONDS") and os.getenv("SCHEDULED_BOT_JITTER_END_SECONDS"):
            return self._run_scheduled_bots_with_jitter()

        """
        Promote objects whose join_at ≤ join_at_threshold.
        Uses SELECT … FOR UPDATE SKIP LOCKED so multiple daemons
        can run safely (e.g. during rolling deploys).
        """

        # Give the bots 5 minutes to spin up, before they join the meeting.
        join_at_upper_threshold = timezone.now() + timezone.timedelta(minutes=5)
        # If we miss a scheduled bot by more than 5 minutes, don't bother launching it, it's a failure and it'll be cleaned up
        # by the clean_up_bots_with_heartbeat_timeout_or_that_never_launched command
        join_at_lower_threshold = timezone.now() - timezone.timedelta(minutes=5)

        with transaction.atomic():
            bots_to_launch = Bot.objects.filter(state=BotStates.SCHEDULED, join_at__lte=join_at_upper_threshold, join_at__gte=join_at_lower_threshold).select_for_update(skip_locked=True)

            for bot in bots_to_launch:
                log.info(f"Launching scheduled bot {bot.id} ({bot.object_id}) with join_at {bot.join_at.isoformat()}")
                launch_scheduled_bot.delay(bot.id, bot.join_at.isoformat())

            log.info("Launched %s bots", len(bots_to_launch))

    def _run_autopay_tasks(self):
        """
        Run autopay tasks for organizations that meet all criteria:
        - Autopay is enabled
        - Has a Stripe customer ID
        - Credit balance is below the threshold
        - No autopay task has been enqueued in the last day
        """
        now = timezone.now()
        cutoff_time = now - timezone.timedelta(days=1)

        # Find organizations that meet all autopay criteria
        organizations = Organization.objects.filter(
            # Autopay must be enabled
            autopay_enabled=True,
            # Must have a Stripe customer ID
            autopay_stripe_customer_id__isnull=False,
            # Credit balance must be below threshold
            centicredits__lt=models.F("autopay_threshold_centricredits"),
            # No charge failure
            autopay_charge_failure_data__isnull=True,
        ).filter(
            # No autopay task enqueued in the last day (or never enqueued)
            Q(autopay_charge_task_enqueued_at__isnull=True) | Q(autopay_charge_task_enqueued_at__lte=cutoff_time)
        )

        for organization in organizations:
            credits = organization.credits()
            threshold = organization.autopay_threshold_credits()
            last_enqueued = organization.autopay_charge_task_enqueued_at.isoformat() if organization.autopay_charge_task_enqueued_at else "never"

            log.info(
                "Enqueueing autopay task for organization %s (credits: %.2f, threshold: %.2f, last enqueued: %s)",
                organization.id,
                credits,
                threshold,
                last_enqueued,
            )

            enqueue_autopay_charge_task(organization)

        log.info("Enqueued %d autopay tasks", len(organizations))

    def _run_stale_transcription_claims(self):
        """Recover regular utterances when a broker delivery or worker dies mid-transcription."""
        now = timezone.now()
        stale_cutoff = now - timezone.timedelta(seconds=TRANSCRIPTION_CLAIM_STALE_SECONDS)
        stale_utterances = (
            Utterance.objects.filter(
                async_transcription__isnull=True,
                transcription__isnull=True,
                failure_data__isnull=True,
            )
            .filter(
                Q(
                    transcription_processing_task_id__isnull=True,
                    created_at__lte=stale_cutoff,
                )
                | Q(
                    transcription_processing_task_id__isnull=False,
                    transcription_processing_started_at__lte=stale_cutoff,
                )
            )
            .order_by("created_at")[:100]
        )

        recovered = 0
        for utterance in stale_utterances:
            recovery_task_id = str(uuid.uuid4())
            claim_filter = Q(
                transcription_processing_task_id__isnull=True,
                created_at__lte=stale_cutoff,
            ) | Q(
                transcription_processing_task_id__isnull=False,
                transcription_processing_started_at__lte=stale_cutoff,
            )

            with transaction.atomic():
                claimed = (
                    Utterance.objects.filter(
                        id=utterance.id,
                        async_transcription__isnull=True,
                        transcription__isnull=True,
                        failure_data__isnull=True,
                    )
                    .filter(claim_filter)
                    .update(
                        transcription_processing_task_id=recovery_task_id,
                        transcription_processing_started_at=now,
                    )
                )
                if not claimed:
                    continue

                def enqueue_recovery(utterance_id=utterance.id, task_id=recovery_task_id):
                    try:
                        process_utterance.apply_async(args=[utterance_id], task_id=task_id)
                    except Exception:
                        Utterance.objects.filter(
                            id=utterance_id,
                            transcription_processing_task_id=task_id,
                        ).update(transcription_processing_started_at=stale_cutoff)
                        log.exception("Could not enqueue stale transcription claim for utterance %s", utterance_id)

                transaction.on_commit(enqueue_recovery)
                recovered += 1

        log.info("Recovered %d stale or missing transcription task(s)", recovered)

    def _run_pending_recording_deliveries(self):
        """Recover finalized or orphaned audio after a bot/container failure."""
        now = timezone.now()
        self._mark_crashed_recording_only_bots(now)
        reenqueue_cutoff = now - timezone.timedelta(seconds=RECORDING_DELIVERY_REENQUEUE_SECONDS)
        active_cutoff = now - timezone.timedelta(seconds=RECORDING_DELIVERY_ACTIVE_SECONDS)
        orphan_cutoff = now - timezone.timedelta(seconds=RECORDING_ORPHAN_RECOVERY_GRACE_SECONDS)

        ready_with_local_spool = (
            Recording.objects.filter(
                transcription_type=TranscriptionTypes.NO_TRANSCRIPTION,
                delivery_state=RecordingDeliveryStates.READY,
                local_file_path__isnull=False,
            )
            .exclude(local_file_path="")
            .order_by("updated_at")
            .values_list("id", flat=True)[:100]
        )
        cleaned = 0
        for recording_id in ready_with_local_spool:
            try:
                if cleanup_ready_recording_spool(recording_id):
                    cleaned += 1
            except Exception:
                log.exception("Could not clean delivered spool file for recording %s", recording_id)

        pending = (
            Recording.objects.filter(transcription_type=TranscriptionTypes.NO_TRANSCRIPTION)
            .filter(
                Q(delivery_state=RecordingDeliveryStates.STAGED)
                & (
                    Q(delivery_enqueued_at__isnull=True)
                    | Q(delivery_enqueued_at__lte=reenqueue_cutoff)
                )
                | Q(delivery_state=RecordingDeliveryStates.UPLOADING)
                & (
                    Q(delivery_started_at__isnull=True)
                    | Q(delivery_started_at__lte=active_cutoff)
                )
            )
            .order_by("updated_at")[:100]
        )
        requeued = 0
        for recording in pending:
            if enqueue_recording_delivery(recording.id):
                requeued += 1

        orphaned = (
            Recording.objects.filter(
                transcription_type=TranscriptionTypes.NO_TRANSCRIPTION,
                bot__state__in=BotStates.post_meeting_states(),
                bot__updated_at__lte=orphan_cutoff,
                delivery_state__in=[RecordingDeliveryStates.NOT_STARTED, RecordingDeliveryStates.FAILED],
                delivery_attempt_count__lt=MAX_RECORDING_DELIVERY_RETRIES,
            )
            .select_related("bot")
            .order_by("updated_at")[:100]
        )
        recovered = 0
        for recording in orphaned:
            try:
                if recover_recording_from_spool(recording.id):
                    recovered += 1
            except Exception:
                log.exception("Could not recover recording %s from the durable spool", recording.id)

        log.info(
            "Cleaned %d delivered spool file(s); requeued %d recording delivery task(s); recovered %d orphaned recording(s)",
            cleaned,
            requeued,
            recovered,
        )

    def _mark_crashed_recording_only_bots(self, now):
        """Move a stale bot to fatal only after Docker confirms it is gone."""
        stale_heartbeat = int(now.timestamp()) - 600
        candidates = (
            Bot.objects.filter(
                recordings__is_default_recording=True,
                recordings__transcription_type=TranscriptionTypes.NO_TRANSCRIPTION,
                last_heartbeat_timestamp__isnull=False,
                last_heartbeat_timestamp__lt=stale_heartbeat,
            )
            .exclude(state__in=BotStates.post_meeting_states())
            .distinct()
            .order_by("last_heartbeat_timestamp")[:50]
        )
        if not candidates:
            return

        try:
            docker_client = docker.from_env()
        except Exception:
            log.exception("Cannot inspect Docker while recovering recording-only bots")
            return

        for bot in candidates:
            try:
                container = docker_client.containers.get(bot.ephemeral_container_name())
                container.reload()
                if container.status == "running":
                    continue
            except docker.errors.NotFound:
                pass
            except Exception:
                log.exception("Cannot inspect container for recording-only bot %s", bot.object_id)
                continue

            try:
                BotEventManager.create_event(
                    bot=bot,
                    event_type=BotEventTypes.FATAL_ERROR,
                    event_sub_type=BotEventSubTypes.FATAL_ERROR_HEARTBEAT_TIMEOUT,
                    event_metadata={"recording_recovery": "container_missing_after_stale_heartbeat"},
                )
                log.warning("Marked crashed recording-only bot %s as fatal for audio recovery", bot.object_id)
            except Exception:
                log.exception("Could not mark recording-only bot %s as fatal", bot.object_id)
