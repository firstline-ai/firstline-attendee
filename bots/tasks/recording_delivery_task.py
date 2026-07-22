"""Crash-safe delivery of complete or partial recording-only audio."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from bots.bot_controller.azure_file_uploader import AzureFileUploader
from bots.bot_controller.s3_file_uploader import S3FileUploader
from bots.models import (
    Credentials,
    Recording,
    RecordingDeliveryStates,
    RecordingStates,
    WebhookTriggerTypes,
)
from bots.webhook_utils import trigger_webhook

logger = logging.getLogger(__name__)

MAX_RECORDING_DELIVERY_RETRIES = 8
RECORDING_DELIVERY_ACTIVE_SECONDS = 900


def recording_spool_directory() -> Path:
    return Path(os.getenv("BOT_RECORDING_SPOOL_DIRECTORY", "/attendee-recording-spool"))


def expected_spool_path(recording: Recording) -> Path:
    filename = f"{recording.bot.object_id}-{recording.object_id}.{recording.bot.recording_format()}"
    return recording_spool_directory() / filename


def _sanitized_failure_data(exc: Exception) -> dict:
    return {
        "error_type": type(exc).__name__,
        "message": "Recording delivery failed; the local audio was preserved for recovery.",
    }


def _enqueue_task(recording_id: int) -> bool:
    try:
        deliver_recording.delay(recording_id)
        return True
    except Exception:
        # STAGED is durable. The scheduler can enqueue it again.
        logger.exception("Could not enqueue recording delivery for recording %s", recording_id)
        return False


def enqueue_recording_delivery(recording_id: int) -> bool:
    """Persist an idempotent claim before publishing the Celery task."""
    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(id=recording_id)
        if recording.delivery_state == RecordingDeliveryStates.READY:
            return False

        active_cutoff = timezone.now() - timezone.timedelta(seconds=RECORDING_DELIVERY_ACTIVE_SECONDS)
        if (
            recording.delivery_state == RecordingDeliveryStates.UPLOADING
            and recording.delivery_started_at
            and recording.delivery_started_at > active_cutoff
        ):
            return False

        recording.delivery_state = RecordingDeliveryStates.STAGED
        recording.delivery_enqueued_at = timezone.now()
        recording.save(update_fields=["delivery_state", "delivery_enqueued_at", "updated_at"])

    return _enqueue_task(recording_id)


def stage_recording_for_delivery(recording_id: int, local_file_path: str, *, is_partial: bool) -> bool:
    """Register a finalized local MP3 before the browser container exits."""
    path = Path(local_file_path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(f"Recording file is unavailable or empty: {path}")

    with transaction.atomic():
        recording = Recording.objects.select_for_update().get(id=recording_id)
        if recording.delivery_state == RecordingDeliveryStates.READY:
            return False
        recording.local_file_path = str(path)
        recording.file_size_bytes = path.stat().st_size
        recording.is_partial = recording.is_partial or is_partial
        recording.delivery_state = RecordingDeliveryStates.STAGED
        recording.delivery_requested_at = recording.delivery_requested_at or timezone.now()
        recording.delivery_failure_data = None
        recording.save(
            update_fields=[
                "local_file_path",
                "file_size_bytes",
                "is_partial",
                "delivery_state",
                "delivery_requested_at",
                "delivery_failure_data",
                "updated_at",
            ]
        )

    enqueue_recording_delivery(recording_id)
    return True


def recover_recording_from_spool(recording_id: int) -> bool:
    """Stage an orphaned MP3 after the bot container died without cleanup."""
    recording = Recording.objects.select_related("bot").get(id=recording_id)
    path = Path(recording.local_file_path) if recording.local_file_path else expected_spool_path(recording)
    try:
        recoverable_file_exists = path.is_file() and path.stat().st_size > 0
    except OSError:
        recoverable_file_exists = False
    if not recoverable_file_exists:
        Recording.objects.filter(
            id=recording.id,
            delivery_state__in=[RecordingDeliveryStates.NOT_STARTED, RecordingDeliveryStates.FAILED],
        ).update(
            delivery_state=RecordingDeliveryStates.FAILED,
            delivery_attempt_count=F("delivery_attempt_count") + 1,
            delivery_failure_data={
                "error_type": "RecordingSpoolUnavailable",
                "message": "Recording delivery failed; no recoverable local audio was found.",
            },
            updated_at=timezone.now(),
        )
        return False
    return stage_recording_for_delivery(recording.id, str(path), is_partial=True)


def _probe_duration_ms(path: Path) -> int:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    duration_seconds = float(result.stdout.strip())
    if duration_seconds <= 0:
        raise ValueError("Recording duration is zero")
    return int(duration_seconds * 1000)


def _repair_mp3_if_needed(path: Path) -> int:
    try:
        return _probe_duration_ms(path)
    except Exception:
        repaired_path = path.with_name(f"{path.stem}.recovered{path.suffix}")
        logger.warning("Attempting to repair interrupted MP3 %s", path)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-c:a", "copy", str(repaired_path)],
            check=True,
            capture_output=True,
            timeout=300,
        )
        if not repaired_path.is_file() or repaired_path.stat().st_size <= 0:
            raise ValueError("Recovered MP3 is empty")
        os.replace(repaired_path, path)
        return _probe_duration_ms(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as audio_file:
        for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _primary_uploader(recording: Recording):
    filename = f"{recording.bot.object_id}-{recording.object_id}.{recording.bot.recording_format()}"
    if settings.STORAGE_PROTOCOL == "azure":
        return AzureFileUploader(
            container=settings.AZURE_RECORDING_STORAGE_CONTAINER_NAME,
            filename=filename,
            connection_string=settings.RECORDING_STORAGE_BACKEND.get("OPTIONS").get("connection_string"),
            account_key=settings.RECORDING_STORAGE_BACKEND.get("OPTIONS").get("account_key"),
            account_name=settings.RECORDING_STORAGE_BACKEND.get("OPTIONS").get("account_name"),
            max_attempts=3,
            initial_retry_delay_seconds=1,
            verify_upload=True,
        )
    return S3FileUploader(
        bucket=settings.AWS_RECORDING_STORAGE_BUCKET_NAME,
        filename=filename,
        endpoint_url=settings.RECORDING_STORAGE_BACKEND.get("OPTIONS").get("endpoint_url"),
        max_attempts=3,
        initial_retry_delay_seconds=1,
        verify_upload=True,
    )


def _upload_primary(recording: Recording, path: Path) -> str:
    uploader = _primary_uploader(recording)
    uploader.upload_file(str(path))
    uploader.wait_for_upload(raise_on_error=True)
    return uploader.filename


def _upload_external(recording: Recording, path: Path) -> str | None:
    bucket = recording.bot.external_media_storage_bucket_name()
    if not bucket:
        return None

    credentials_record = recording.bot.project.credentials.filter(
        credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE
    ).first()
    if not credentials_record:
        raise RuntimeError("External media storage credentials are not configured")
    credentials = credentials_record.get_credentials()
    if not credentials:
        raise RuntimeError("External media storage credentials are empty")

    object_key = recording.bot.external_media_storage_recording_file_name() or path.name
    uploader = S3FileUploader(
        bucket=bucket,
        filename=object_key,
        endpoint_url=credentials.get("endpoint_url") or None,
        region_name=credentials.get("region_name"),
        access_key_id=credentials.get("access_key_id"),
        access_key_secret=credentials.get("access_key_secret"),
        max_attempts=3,
        initial_retry_delay_seconds=1,
        verify_upload=True,
    )
    uploader.upload_file(str(path))
    uploader.wait_for_upload(raise_on_error=True)
    return object_key


def _claim_delivery(recording_id: int) -> Recording | None:
    with transaction.atomic():
        recording = Recording.objects.select_for_update().select_related("bot__project").get(id=recording_id)
        if recording.delivery_state == RecordingDeliveryStates.READY:
            return None
        active_cutoff = timezone.now() - timezone.timedelta(seconds=RECORDING_DELIVERY_ACTIVE_SECONDS)
        if (
            recording.delivery_state == RecordingDeliveryStates.UPLOADING
            and recording.delivery_started_at
            and recording.delivery_started_at > active_cutoff
        ):
            return None
        recording.delivery_state = RecordingDeliveryStates.UPLOADING
        recording.delivery_started_at = timezone.now()
        recording.delivery_attempt_count += 1
        recording.save(
            update_fields=[
                "delivery_state",
                "delivery_started_at",
                "delivery_attempt_count",
                "updated_at",
            ]
        )
        return recording


def _mark_failed(recording_id: int, exc: Exception, *, terminal: bool) -> None:
    Recording.objects.filter(id=recording_id).update(
        delivery_state=RecordingDeliveryStates.FAILED if terminal else RecordingDeliveryStates.STAGED,
        delivery_failure_data=_sanitized_failure_data(exc),
        updated_at=timezone.now(),
    )


def _mark_ready(
    recording_id: int,
    *,
    primary_key: str,
    external_key: str | None,
    file_size_bytes: int,
    file_sha256: str,
    duration_ms: int,
) -> None:
    with transaction.atomic():
        recording = Recording.objects.select_for_update().select_related("bot").get(id=recording_id)
        if recording.delivery_state == RecordingDeliveryStates.READY:
            return
        completed_at = timezone.now()
        recording.file = primary_key
        recording.external_storage_key = external_key
        recording.local_file_path = None
        recording.file_size_bytes = file_size_bytes
        recording.file_sha256 = file_sha256
        recording.duration_ms = duration_ms
        recording.delivery_state = RecordingDeliveryStates.READY
        recording.delivery_completed_at = completed_at
        recording.delivery_failure_data = None
        recording.state = RecordingStates.COMPLETE
        recording.completed_at = recording.completed_at or completed_at
        recording.save(
            update_fields=[
                "file",
                "external_storage_key",
                "local_file_path",
                "file_size_bytes",
                "file_sha256",
                "duration_ms",
                "delivery_state",
                "delivery_completed_at",
                "delivery_failure_data",
                "state",
                "completed_at",
                "updated_at",
            ]
        )
        trigger_webhook(
            webhook_trigger_type=WebhookTriggerTypes.RECORDING_READY,
            bot=recording.bot,
            payload={
                "recording_id": recording.object_id,
                "state": "ready",
                "format": recording.bot.recording_format(),
                "is_partial": recording.is_partial,
                "duration_ms": duration_ms,
                "file_size_bytes": file_size_bytes,
                "sha256": file_sha256,
                "storage": {
                    "type": "external" if external_key else "primary",
                    "object_key": external_key or primary_key,
                    "bucket_name": recording.bot.external_media_storage_bucket_name() if external_key else None,
                },
            },
        )


@shared_task(bind=True, max_retries=MAX_RECORDING_DELIVERY_RETRIES)
def deliver_recording(self, recording_id: int) -> None:
    """Validate, persist and announce one logical audio recording."""
    recording = _claim_delivery(recording_id)
    if recording is None:
        return

    path = Path(recording.local_file_path) if recording.local_file_path else expected_spool_path(recording)
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError(f"Recording spool file is unavailable: {path}")
        duration_ms = _repair_mp3_if_needed(path)
        file_size_bytes = path.stat().st_size
        file_sha256 = _sha256(path)
        primary_key = _upload_primary(recording, path)
        external_key = _upload_external(recording, path)
        _mark_ready(
            recording.id,
            primary_key=primary_key,
            external_key=external_key,
            file_size_bytes=file_size_bytes,
            file_sha256=file_sha256,
            duration_ms=duration_ms,
        )
        path.unlink(missing_ok=True)
        logger.info("Recording %s is ready for post-meeting processing", recording.id)
    except Exception as exc:
        terminal = self.request.retries >= self.max_retries
        _mark_failed(recording.id, exc, terminal=terminal)
        if terminal:
            logger.exception("Recording delivery exhausted retries for recording %s", recording.id)
            raise
        countdown = min(300, 2 ** max(0, self.request.retries))
        logger.warning("Recording delivery failed for %s; retrying in %ss", recording.id, countdown)
        raise self.retry(exc=exc, countdown=countdown)
