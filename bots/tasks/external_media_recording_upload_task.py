"""Reliable delivery of completed recordings to customer-controlled S3 storage."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from bots.bot_controller.s3_file_uploader import S3FileUploader
from bots.models import Credentials, ExternalMediaUploadStates, Recording

logger = logging.getLogger(__name__)

MAX_EXTERNAL_MEDIA_UPLOAD_RETRIES = 8
EXTERNAL_MEDIA_UPLOAD_ACTIVE_SECONDS = 900


def _failure_data(exc: Exception) -> dict:
    # Do not persist endpoint URLs, credential values or provider response data.
    return {"error_type": type(exc).__name__, "message": "External media upload failed"}


def enqueue_external_media_recording_upload(recording_id: int) -> bool:
    """Queue delivery after the primary recording has been persisted."""
    with transaction.atomic():
        recording = Recording.objects.select_for_update().select_related("bot__project").get(id=recording_id)
        if not recording.bot.external_media_storage_bucket_name() or not recording.file:
            return False
        if recording.external_media_upload_state == ExternalMediaUploadStates.COMPLETE:
            return False
        active_cutoff = timezone.now() - timezone.timedelta(seconds=EXTERNAL_MEDIA_UPLOAD_ACTIVE_SECONDS)
        if (
            recording.external_media_upload_state == ExternalMediaUploadStates.UPLOADING
            and recording.external_media_upload_started_at
            and recording.external_media_upload_started_at > active_cutoff
        ):
            return False
        recording.external_media_upload_state = ExternalMediaUploadStates.PENDING
        recording.external_media_upload_enqueued_at = timezone.now()
        recording.save(update_fields=["external_media_upload_state", "external_media_upload_enqueued_at", "updated_at"])

    try:
        upload_recording_to_external_media.delay(recording_id)
        return True
    except Exception:
        # The scheduler will see the persistent PENDING state and try again.
        logger.exception("Could not enqueue external-media upload for recording %s", recording_id)
        return False


def _mark_upload_started(recording_id: int) -> Recording | None:
    with transaction.atomic():
        recording = Recording.objects.select_for_update().select_related("bot__project").get(id=recording_id)
        if recording.external_media_upload_state == ExternalMediaUploadStates.COMPLETE:
            return None
        if not recording.bot.external_media_storage_bucket_name() or not recording.file:
            return None
        active_cutoff = timezone.now() - timezone.timedelta(seconds=EXTERNAL_MEDIA_UPLOAD_ACTIVE_SECONDS)
        if (
            recording.external_media_upload_state == ExternalMediaUploadStates.UPLOADING
            and recording.external_media_upload_started_at
            and recording.external_media_upload_started_at > active_cutoff
        ):
            return None
        recording.external_media_upload_state = ExternalMediaUploadStates.UPLOADING
        recording.external_media_upload_started_at = timezone.now()
        recording.external_media_upload_attempt_count += 1
        recording.save(
            update_fields=[
                "external_media_upload_state",
                "external_media_upload_started_at",
                "external_media_upload_attempt_count",
                "updated_at",
            ]
        )
        return recording


def _mark_upload_complete(recording_id: int) -> None:
    Recording.objects.filter(id=recording_id).update(
        external_media_upload_state=ExternalMediaUploadStates.COMPLETE,
        external_media_upload_completed_at=timezone.now(),
        external_media_upload_failure_data=None,
        updated_at=timezone.now(),
    )


def _mark_upload_failed(recording_id: int, exc: Exception, terminal: bool) -> None:
    Recording.objects.filter(id=recording_id).update(
        external_media_upload_state=(
            ExternalMediaUploadStates.FAILED if terminal else ExternalMediaUploadStates.PENDING
        ),
        external_media_upload_failure_data=_failure_data(exc),
        updated_at=timezone.now(),
    )


def _upload_to_external_storage(recording: Recording) -> None:
    credentials_record = recording.bot.project.credentials.filter(
        credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE
    ).first()
    if not credentials_record:
        raise RuntimeError("External media storage credentials are not configured")
    credentials = credentials_record.get_credentials()
    if not credentials:
        raise RuntimeError("External media storage credentials are empty")

    bucket = recording.bot.external_media_storage_bucket_name()
    filename = recording.bot.external_media_storage_recording_file_name() or recording.file.name.rsplit("/", 1)[-1]
    suffix = Path(filename).suffix or ".mp3"

    with tempfile.NamedTemporaryFile(prefix="attendee-recording-", suffix=suffix) as temporary_file:
        with recording.file.open("rb") as source:
            shutil.copyfileobj(source, temporary_file)
        temporary_file.flush()

        uploader = S3FileUploader(
            bucket=bucket,
            filename=filename,
            endpoint_url=credentials.get("endpoint_url") or None,
            region_name=credentials.get("region_name"),
            access_key_id=credentials.get("access_key_id"),
            access_key_secret=credentials.get("access_key_secret"),
            max_attempts=3,
            initial_retry_delay_seconds=1,
            verify_upload=True,
        )
        uploader.upload_file(temporary_file.name)
        uploader.wait_for_upload(raise_on_error=True)


@shared_task(bind=True, max_retries=MAX_EXTERNAL_MEDIA_UPLOAD_RETRIES)
def upload_recording_to_external_media(self, recording_id: int) -> None:
    """Copy a primary recording to external storage with durable state and retry."""
    recording = _mark_upload_started(recording_id)
    if recording is None:
        return

    try:
        _upload_to_external_storage(recording)
    except Exception as exc:
        terminal = self.request.retries >= self.max_retries
        _mark_upload_failed(recording_id, exc, terminal=terminal)
        if terminal:
            logger.exception("External-media upload exhausted retries for recording %s", recording_id)
            raise
        countdown = min(300, 2 ** max(0, self.request.retries))
        logger.warning(
            "External-media upload failed for recording %s; retrying in %ss",
            recording_id,
            countdown,
        )
        raise self.retry(exc=exc, countdown=countdown)

    _mark_upload_complete(recording_id)
    logger.info("External-media upload complete for recording %s", recording_id)
