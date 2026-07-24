import json
import tempfile
import uuid
from collections import namedtuple
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from kombu.transport.redis import Channel as RedisChannel

from accounts.models import Organization
from bots.models import (
    Bot,
    BotStates,
    Project,
    Recording,
    RecordingDeliveryStates,
    RecordingStates,
    RecordingTypes,
    TranscriptionTypes,
    WebhookDeliveryAttempt,
    WebhookDeliveryAttemptStatus,
    WebhookSubscription,
    WebhookTriggerTypes,
)

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


class FakeRedis:
    def __init__(self, values):
        self.values = values
        self.closed = False

    def llen(self, key):
        return self.values.get(key, 0)

    def close(self):
        self.closed = True


class ReliabilityAuditCommandTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        organization = Organization.objects.create(name="Audit Organization")
        self.project = Project.objects.create(
            name="Audit Project",
            organization=organization,
        )
        self.stale_bot = Bot.objects.create(
            project=self.project,
            name="Stale bot",
            meeting_url="https://meet.google.com/secret-meeting",
            state=BotStates.JOINED_RECORDING,
            last_heartbeat_timestamp=int(self.now.timestamp()) - 1000,
        )
        recent_bot = Bot.objects.create(
            project=self.project,
            name="Recent bot",
            meeting_url="https://meet.google.com/recent-meeting",
            state=BotStates.JOINED_RECORDING,
            last_heartbeat_timestamp=int(self.now.timestamp()),
        )
        self.staged_recording = self._create_recording(
            self.stale_bot,
            RecordingDeliveryStates.STAGED,
        )
        Recording.objects.filter(id=self.staged_recording.id).update(
            delivery_requested_at=self.now - timezone.timedelta(minutes=30),
            updated_at=self.now,
        )
        self.uploading_recording = self._create_recording(
            recent_bot,
            RecordingDeliveryStates.UPLOADING,
        )
        Recording.objects.filter(id=self.uploading_recording.id).update(
            delivery_started_at=self.now - timezone.timedelta(minutes=30),
            updated_at=self.now,
        )
        self._create_recording(
            recent_bot,
            RecordingDeliveryStates.FAILED,
        )

        subscription = WebhookSubscription.objects.create(
            project=self.project,
            url="https://secret.example/webhook",
            triggers=[WebhookTriggerTypes.RECORDING_READY],
        )
        WebhookDeliveryAttempt.objects.create(
            webhook_subscription=subscription,
            webhook_trigger_type=WebhookTriggerTypes.RECORDING_READY,
            idempotency_key=uuid.uuid4(),
            bot=self.stale_bot,
            payload={"secret_marker": "must-never-be-printed"},
            status=WebhookDeliveryAttemptStatus.FAILURE,
            attempt_count=1,
        )
        WebhookDeliveryAttempt.objects.create(
            webhook_subscription=subscription,
            webhook_trigger_type=WebhookTriggerTypes.RECORDING_READY,
            idempotency_key=uuid.uuid4(),
            bot=self.stale_bot,
            payload={"secret_marker": "must-never-be-printed"},
            status=WebhookDeliveryAttemptStatus.FAILURE,
            attempt_count=3,
        )

    @staticmethod
    def _create_recording(bot, delivery_state):
        return Recording.objects.create(
            bot=bot,
            recording_type=RecordingTypes.AUDIO_ONLY,
            transcription_type=TranscriptionTypes.NO_TRANSCRIPTION,
            transcription_provider=None,
            is_default_recording=True,
            state=RecordingStates.FAILED,
            delivery_state=delivery_state,
        )

    @staticmethod
    def _noncritical_options(spool_path):
        return {
            "spool_path": spool_path,
            "disk_path": "/",
            "max_celery_queue": 100,
            "max_launcher_queue": 100,
            "max_webhooks_queue": 100,
            "max_stuck_recordings": 100,
            "max_failed_recordings": 100,
            "max_failed_webhooks": 100,
            "max_exhausted_webhooks": 100,
            "max_stale_bots": 100,
            "max_spool_files": 100,
            "max_spool_bytes": 10000,
            "max_zero_byte_files": 100,
            "min_disk_free_gib": 0,
            "max_collection_errors": 0,
        }

    @staticmethod
    def _fake_redis():
        return FakeRedis(
            {
                "celery": 2,
                f"celery{RedisChannel.sep}3": 1,
                "bot_launcher_vm": 4,
                "webhooks": 5,
            }
        )

    def test_json_audit_reports_all_metrics_without_sensitive_data_or_writes(self):
        with tempfile.TemporaryDirectory() as spool_directory:
            Path(spool_directory, "meeting-1.mp3").write_bytes(b"audio")
            Path(spool_directory, "meeting-2.mp3").touch()
            fake_redis = self._fake_redis()
            output = StringIO()
            options = self._noncritical_options(spool_directory)

            with (
                patch(
                    "bots.management.commands.reliability_audit.redis.from_url",
                    return_value=fake_redis,
                ),
                patch(
                    "bots.management.commands.reliability_audit.shutil.disk_usage",
                    return_value=DiskUsage(
                        total=100 * 1024**3,
                        used=80 * 1024**3,
                        free=20 * 1024**3,
                    ),
                ),
                patch(
                    "bots.management.commands.reliability_audit.timezone.now",
                    return_value=self.now,
                ),
                CaptureQueriesContext(connection) as queries,
            ):
                call_command(
                    "reliability_audit",
                    as_json=True,
                    stdout=output,
                    **options,
                )

            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "ok")
            self.assertEqual(
                report["queues"]["depths"],
                {
                    "celery": 3,
                    "bot_launcher_vm": 4,
                    "webhooks": 5,
                },
            )
            self.assertEqual(report["recordings"]["staged_old"], 1)
            self.assertEqual(report["recordings"]["uploading_old"], 1)
            self.assertEqual(report["recordings"]["failed_recent"], 1)
            self.assertEqual(report["webhooks"]["failed_recent"], 2)
            self.assertEqual(report["webhooks"]["retrying_recent"], 1)
            self.assertEqual(report["webhooks"]["exhausted_recent"], 1)
            self.assertEqual(report["bots"]["stale_active"], 1)
            self.assertEqual(report["spool"]["files"], 2)
            self.assertEqual(report["spool"]["bytes"], 5)
            self.assertEqual(report["spool"]["zero_byte_files"], 1)
            self.assertEqual(report["disk"]["free_bytes"], 20 * 1024**3)
            self.assertTrue(fake_redis.closed)

            serialized = output.getvalue()
            self.assertNotIn("secret.example", serialized)
            self.assertNotIn("secret-meeting", serialized)
            self.assertNotIn("must-never-be-printed", serialized)
            write_queries = [query["sql"] for query in queries.captured_queries if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
            self.assertEqual(write_queries, [])

    def test_human_audit_is_readable_and_sanitized(self):
        with tempfile.TemporaryDirectory() as spool_directory:
            output = StringIO()
            options = self._noncritical_options(spool_directory)

            with (
                patch(
                    "bots.management.commands.reliability_audit.redis.from_url",
                    return_value=self._fake_redis(),
                ),
                patch(
                    "bots.management.commands.reliability_audit.shutil.disk_usage",
                    return_value=DiskUsage(total=100, used=20, free=80),
                ),
                patch(
                    "bots.management.commands.reliability_audit.timezone.now",
                    return_value=self.now,
                ),
            ):
                call_command(
                    "reliability_audit",
                    stdout=output,
                    **options,
                )

            rendered = output.getvalue()
            self.assertIn("Attendee reliability audit: OK", rendered)
            self.assertIn(
                "Queues: celery=3, bot_launcher_vm=4, webhooks=5",
                rendered,
            )
            self.assertIn(
                "Recordings: staged_old=1, uploading_old=1, failed_recent=1",
                rendered,
            )
            self.assertIn("Bots: stale_active=1", rendered)
            self.assertNotIn("secret.example", rendered)
            self.assertNotIn("secret-meeting", rendered)
            self.assertNotIn("must-never-be-printed", rendered)

    def test_critical_threshold_writes_report_then_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as spool_directory:
            output = StringIO()
            options = self._noncritical_options(spool_directory)
            options["max_stale_bots"] = 0

            with (
                patch(
                    "bots.management.commands.reliability_audit.redis.from_url",
                    return_value=self._fake_redis(),
                ),
                patch(
                    "bots.management.commands.reliability_audit.shutil.disk_usage",
                    return_value=DiskUsage(total=100, used=20, free=80),
                ),
                patch(
                    "bots.management.commands.reliability_audit.timezone.now",
                    return_value=self.now,
                ),
                self.assertRaises(CommandError),
            ):
                call_command(
                    "reliability_audit",
                    as_json=True,
                    stdout=output,
                    **options,
                )

            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "critical")
            self.assertIn(
                {
                    "metric": "bots.stale_active",
                    "value": 1,
                    "maximum": 0,
                },
                report["critical_reasons"],
            )

    def test_collection_errors_are_sanitized_and_thresholded(self):
        with tempfile.TemporaryDirectory() as spool_directory:
            output = StringIO()
            options = self._noncritical_options(spool_directory)
            options["max_collection_errors"] = 1

            with (
                patch(
                    "bots.management.commands.reliability_audit.redis.from_url",
                    side_effect=RuntimeError("redis://user:secret-password@private-host"),
                ),
                patch(
                    "bots.management.commands.reliability_audit.shutil.disk_usage",
                    return_value=DiskUsage(total=100, used=20, free=80),
                ),
                patch(
                    "bots.management.commands.reliability_audit.timezone.now",
                    return_value=self.now,
                ),
            ):
                call_command(
                    "reliability_audit",
                    as_json=True,
                    stdout=output,
                    **options,
                )

            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "ok")
            self.assertFalse(report["queues"]["available"])
            self.assertEqual(
                report["queues"]["error_type"],
                "RuntimeError",
            )
            self.assertEqual(
                report["collection_errors"],
                [{"component": "queues", "error_type": "RuntimeError"}],
            )
            self.assertNotIn("secret-password", output.getvalue())
            self.assertNotIn("private-host", output.getvalue())
