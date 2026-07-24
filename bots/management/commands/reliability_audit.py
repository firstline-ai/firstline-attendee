"""Read-only operational reliability audit for Attendee."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from pathlib import Path

import redis
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone
from kombu.transport.redis import Channel as RedisChannel

from bots.models import (
    Bot,
    BotStates,
    Recording,
    RecordingDeliveryStates,
    WebhookDeliveryAttempt,
    WebhookDeliveryAttemptStatus,
)

GIB = 1024**3
QUEUE_NAMES = ("celery", "bot_launcher_vm", "webhooks")
ACTIVE_BOT_STATES = (
    BotStates.JOINING,
    BotStates.JOINED_NOT_RECORDING,
    BotStates.JOINED_RECORDING,
    BotStates.LEAVING,
    BotStates.POST_PROCESSING,
    BotStates.WAITING_ROOM,
    BotStates.JOINED_RECORDING_PAUSED,
    BotStates.JOINING_BREAKOUT_ROOM,
    BotStates.LEAVING_BREAKOUT_ROOM,
    BotStates.JOINED_RECORDING_PERMISSION_DENIED,
    BotStates.CONNECTING,
    BotStates.CONNECTED,
    BotStates.DISCONNECTING,
)


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


class Command(BaseCommand):
    help = "Reports read-only Attendee reliability metrics and critical thresholds."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument(
            "--spool-path",
            default=os.getenv(
                "BOT_RECORDING_SPOOL_DIRECTORY",
                "/attendee-recording-spool",
            ),
        )
        parser.add_argument(
            "--disk-path",
            default=None,
            help="Filesystem path to inspect (defaults to --spool-path).",
        )
        parser.add_argument("--recording-stale-minutes", type=nonnegative_int, default=15)
        parser.add_argument("--recording-failed-window-minutes", type=nonnegative_int, default=60)
        parser.add_argument("--webhook-window-minutes", type=nonnegative_int, default=60)
        parser.add_argument("--webhook-max-attempts", type=nonnegative_int, default=3)
        parser.add_argument("--heartbeat-stale-seconds", type=nonnegative_int, default=600)
        parser.add_argument("--max-celery-queue", type=nonnegative_int, default=100)
        parser.add_argument("--max-launcher-queue", type=nonnegative_int, default=8)
        parser.add_argument("--max-webhooks-queue", type=nonnegative_int, default=100)
        parser.add_argument("--max-stuck-recordings", type=nonnegative_int, default=0)
        parser.add_argument("--max-failed-recordings", type=nonnegative_int, default=5)
        parser.add_argument("--max-failed-webhooks", type=nonnegative_int, default=10)
        parser.add_argument("--max-exhausted-webhooks", type=nonnegative_int, default=0)
        parser.add_argument("--max-stale-bots", type=nonnegative_int, default=0)
        parser.add_argument("--max-spool-files", type=nonnegative_int, default=1000)
        parser.add_argument("--max-spool-bytes", type=nonnegative_int, default=5 * GIB)
        parser.add_argument("--max-zero-byte-files", type=nonnegative_int, default=10)
        parser.add_argument("--min-disk-free-gib", type=nonnegative_float, default=10)
        parser.add_argument("--max-collection-errors", type=nonnegative_int, default=0)

    def handle(self, *args, **options):
        now = timezone.now()
        collection_errors = []
        report = {
            "generated_at": now.isoformat(),
            "queues": self._safe_collect(
                "queues",
                self._collect_queues,
                collection_errors,
            ),
            "recordings": self._safe_collect(
                "recordings",
                lambda: self._collect_recordings(now, options),
                collection_errors,
            ),
            "webhooks": self._safe_collect(
                "webhooks",
                lambda: self._collect_webhooks(now, options),
                collection_errors,
            ),
            "bots": self._safe_collect(
                "bots",
                lambda: self._collect_bots(now, options),
                collection_errors,
            ),
            "spool": self._safe_collect(
                "spool",
                lambda: self._collect_spool(Path(options["spool_path"])),
                collection_errors,
            ),
            "disk": self._safe_collect(
                "disk",
                lambda: self._collect_disk(Path(options["disk_path"] or options["spool_path"])),
                collection_errors,
            ),
        }
        report["collection_errors"] = collection_errors
        report["thresholds"] = self._thresholds(options)
        critical_reasons = self._critical_reasons(report)
        report["critical_reasons"] = critical_reasons
        report["status"] = "critical" if critical_reasons else "ok"

        if options["as_json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
        else:
            self._write_human(report)

        if critical_reasons:
            raise CommandError("Critical reliability thresholds exceeded.")

    def _safe_collect(self, component, collector, collection_errors):
        try:
            return {"available": True, **collector()}
        except Exception as exc:
            error = {
                "component": component,
                "error_type": type(exc).__name__,
            }
            collection_errors.append(error)
            return {"available": False, "error_type": error["error_type"]}

    def _collect_queues(self):
        client = redis.from_url(settings.REDIS_URL_WITH_PARAMS)
        try:
            depths = {}
            for queue_name in QUEUE_NAMES:
                depths[queue_name] = sum(int(client.llen(queue_key)) for queue_key in self._redis_queue_keys(queue_name))
            return {"depths": depths}
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    @staticmethod
    def _redis_queue_keys(queue_name):
        for priority in RedisChannel.priority_steps:
            if priority == 0:
                yield queue_name
            else:
                yield f"{queue_name}{RedisChannel.sep}{priority}"

    @staticmethod
    def _collect_recordings(now, options):
        stale_cutoff = now - timezone.timedelta(minutes=options["recording_stale_minutes"])
        failed_cutoff = now - timezone.timedelta(minutes=options["recording_failed_window_minutes"])
        return {
            "staged_old": Recording.objects.filter(delivery_state=RecordingDeliveryStates.STAGED)
            .filter(
                Q(delivery_requested_at__lte=stale_cutoff)
                | Q(
                    delivery_requested_at__isnull=True,
                    updated_at__lte=stale_cutoff,
                )
            )
            .count(),
            "uploading_old": Recording.objects.filter(delivery_state=RecordingDeliveryStates.UPLOADING)
            .filter(
                Q(delivery_started_at__lte=stale_cutoff)
                | Q(
                    delivery_started_at__isnull=True,
                    updated_at__lte=stale_cutoff,
                )
            )
            .count(),
            "failed_recent": Recording.objects.filter(
                delivery_state=RecordingDeliveryStates.FAILED,
                updated_at__gte=failed_cutoff,
            ).count(),
        }

    @staticmethod
    def _collect_webhooks(now, options):
        recent_cutoff = now - timezone.timedelta(minutes=options["webhook_window_minutes"])
        recent = WebhookDeliveryAttempt.objects.filter(updated_at__gte=recent_cutoff)
        failed = recent.filter(status=WebhookDeliveryAttemptStatus.FAILURE)
        max_attempts = options["webhook_max_attempts"]
        retrying = recent.filter(
            Q(
                status=WebhookDeliveryAttemptStatus.FAILURE,
                attempt_count__lt=max_attempts,
            )
            | Q(
                status=WebhookDeliveryAttemptStatus.PENDING,
                attempt_count__gt=0,
            )
        )
        return {
            "failed_recent": failed.count(),
            "retrying_recent": retrying.count(),
            "exhausted_recent": failed.filter(attempt_count__gte=max_attempts).count(),
        }

    @staticmethod
    def _collect_bots(now, options):
        heartbeat_cutoff = int(now.timestamp()) - options["heartbeat_stale_seconds"]
        updated_cutoff = now - timezone.timedelta(seconds=options["heartbeat_stale_seconds"])
        stale_active = Bot.objects.filter(state__in=ACTIVE_BOT_STATES).filter(
            Q(last_heartbeat_timestamp__lt=heartbeat_cutoff)
            | Q(
                last_heartbeat_timestamp__isnull=True,
                updated_at__lte=updated_cutoff,
            )
        )
        return {"stale_active": stale_active.count()}

    @staticmethod
    def _collect_spool(path):
        files = 0
        total_bytes = 0
        zero_byte_files = 0
        with os.scandir(path) as entries:
            for entry in entries:
                file_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                files += 1
                total_bytes += file_stat.st_size
                if file_stat.st_size == 0:
                    zero_byte_files += 1
        return {
            "path": str(path),
            "files": files,
            "bytes": total_bytes,
            "zero_byte_files": zero_byte_files,
        }

    @staticmethod
    def _collect_disk(path):
        usage = shutil.disk_usage(path)
        return {
            "path": str(path),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }

    @staticmethod
    def _thresholds(options):
        return {
            "windows": {
                "recording_stale_minutes": options["recording_stale_minutes"],
                "recording_failed_minutes": options["recording_failed_window_minutes"],
                "webhook_recent_minutes": options["webhook_window_minutes"],
                "heartbeat_stale_seconds": options["heartbeat_stale_seconds"],
            },
            "max_queue_depth": {
                "celery": options["max_celery_queue"],
                "bot_launcher_vm": options["max_launcher_queue"],
                "webhooks": options["max_webhooks_queue"],
            },
            "max_stuck_recordings": options["max_stuck_recordings"],
            "max_failed_recordings": options["max_failed_recordings"],
            "max_failed_webhooks": options["max_failed_webhooks"],
            "max_exhausted_webhooks": options["max_exhausted_webhooks"],
            "max_stale_bots": options["max_stale_bots"],
            "max_spool_files": options["max_spool_files"],
            "max_spool_bytes": options["max_spool_bytes"],
            "max_zero_byte_files": options["max_zero_byte_files"],
            "min_disk_free_bytes": int(options["min_disk_free_gib"] * GIB),
            "max_collection_errors": options["max_collection_errors"],
        }

    def _critical_reasons(self, report):
        thresholds = report["thresholds"]
        reasons = []
        if report["queues"]["available"]:
            for queue_name, depth in report["queues"]["depths"].items():
                self._add_max_reason(
                    reasons,
                    f"queues.{queue_name}",
                    depth,
                    thresholds["max_queue_depth"][queue_name],
                )
        if report["recordings"]["available"]:
            stuck = report["recordings"]["staged_old"] + report["recordings"]["uploading_old"]
            self._add_max_reason(
                reasons,
                "recordings.stuck",
                stuck,
                thresholds["max_stuck_recordings"],
            )
            self._add_max_reason(
                reasons,
                "recordings.failed_recent",
                report["recordings"]["failed_recent"],
                thresholds["max_failed_recordings"],
            )
        if report["webhooks"]["available"]:
            self._add_max_reason(
                reasons,
                "webhooks.failed_recent",
                report["webhooks"]["failed_recent"],
                thresholds["max_failed_webhooks"],
            )
            self._add_max_reason(
                reasons,
                "webhooks.exhausted_recent",
                report["webhooks"]["exhausted_recent"],
                thresholds["max_exhausted_webhooks"],
            )
        if report["bots"]["available"]:
            self._add_max_reason(
                reasons,
                "bots.stale_active",
                report["bots"]["stale_active"],
                thresholds["max_stale_bots"],
            )
        if report["spool"]["available"]:
            self._add_max_reason(
                reasons,
                "spool.files",
                report["spool"]["files"],
                thresholds["max_spool_files"],
            )
            self._add_max_reason(
                reasons,
                "spool.bytes",
                report["spool"]["bytes"],
                thresholds["max_spool_bytes"],
            )
            self._add_max_reason(
                reasons,
                "spool.zero_byte_files",
                report["spool"]["zero_byte_files"],
                thresholds["max_zero_byte_files"],
            )
        if report["disk"]["available"]:
            free_bytes = report["disk"]["free_bytes"]
            minimum = thresholds["min_disk_free_bytes"]
            if free_bytes < minimum:
                reasons.append(
                    {
                        "metric": "disk.free_bytes",
                        "value": free_bytes,
                        "minimum": minimum,
                    }
                )
        self._add_max_reason(
            reasons,
            "collection_errors",
            len(report["collection_errors"]),
            thresholds["max_collection_errors"],
        )
        return reasons

    @staticmethod
    def _add_max_reason(reasons, metric, value, maximum):
        if value > maximum:
            reasons.append(
                {
                    "metric": metric,
                    "value": value,
                    "maximum": maximum,
                }
            )

    def _write_human(self, report):
        self.stdout.write(f"Attendee reliability audit: {report['status'].upper()}")
        self.stdout.write(f"Generated at: {report['generated_at']}")
        self._write_component(
            "Queues",
            report["queues"],
            lambda data: ", ".join(f"{name}={depth}" for name, depth in data["depths"].items()),
        )
        self._write_component(
            "Recordings",
            report["recordings"],
            lambda data: (f"staged_old={data['staged_old']}, uploading_old={data['uploading_old']}, failed_recent={data['failed_recent']}"),
        )
        self._write_component(
            "Webhooks",
            report["webhooks"],
            lambda data: (f"failed_recent={data['failed_recent']}, retrying_recent={data['retrying_recent']}, exhausted_recent={data['exhausted_recent']}"),
        )
        self._write_component(
            "Bots",
            report["bots"],
            lambda data: f"stale_active={data['stale_active']}",
        )
        self._write_component(
            "Spool",
            report["spool"],
            lambda data: (f"files={data['files']}, bytes={data['bytes']}, zero_byte_files={data['zero_byte_files']}"),
        )
        self._write_component(
            "Disk",
            report["disk"],
            lambda data: (f"free_bytes={data['free_bytes']}, total_bytes={data['total_bytes']}"),
        )
        self.stdout.write(f"Collection errors: {len(report['collection_errors'])}")
        if report["critical_reasons"]:
            self.stdout.write("Critical thresholds:")
            for reason in report["critical_reasons"]:
                boundary_name = "maximum" if "maximum" in reason else "minimum"
                self.stdout.write(f"- {reason['metric']}={reason['value']} ({boundary_name}={reason[boundary_name]})")

    def _write_component(self, label, component, formatter):
        if component["available"]:
            self.stdout.write(f"{label}: {formatter(component)}")
        else:
            self.stdout.write(f"{label}: unavailable ({component['error_type']})")
