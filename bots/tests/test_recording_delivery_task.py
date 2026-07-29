import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import docker
from celery.exceptions import Retry
from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import Organization
from bots.management.commands.run_scheduler import Command
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
    WebhookSubscription,
    WebhookTriggerTypes,
)
from bots.tasks.recording_delivery_task import (
    MAX_RECORDING_DELIVERY_RETRIES,
    _claim_delivery,
    cleanup_ready_recording_spool,
    deliver_recording,
    expected_spool_path,
    recover_recording_from_spool,
)


class RecordingDeliveryTaskTests(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Recording Delivery Organization")
        self.project = Project.objects.create(name="Recording Delivery Project", organization=organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Recorder",
            meeting_url="https://meet.google.com/abc-defg-hij",
            state=BotStates.ENDED,
            settings={
                "recording_settings": {"format": "mp3"},
                "transcription_settings": {"none": {}},
                "external_media_storage_settings": {"bucket_name": "recordings"},
            },
        )
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_ONLY,
            transcription_type=TranscriptionTypes.NO_TRANSCRIPTION,
            transcription_provider=None,
            is_default_recording=True,
            state=RecordingStates.FAILED,
        )
        WebhookSubscription.objects.create(
            project=self.project,
            bot=self.bot,
            url="https://example.com/recording-ready",
            triggers=[WebhookTriggerTypes.RECORDING_READY],
        )

    @patch("bots.tasks.recording_delivery_task._upload_external", return_value="meetings/test.mp3")
    @patch("bots.tasks.recording_delivery_task._upload_primary", return_value="primary/test.mp3")
    @patch("bots.tasks.recording_delivery_task._repair_mp3_if_needed", return_value=12345)
    def test_delivery_marks_recording_ready_and_is_idempotent(self, _mock_repair, mock_primary, mock_external):
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "recording.mp3"
            path.write_bytes(b"complete meeting audio")
            self.recording.local_file_path = str(path)
            self.recording.delivery_state = RecordingDeliveryStates.STAGED
            self.recording.save(update_fields=["local_file_path", "delivery_state", "updated_at"])

            deliver_recording.run(self.recording.id)

            self.recording.refresh_from_db()
            self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.READY)
            self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
            self.assertEqual(self.recording.file.name, "primary/test.mp3")
            self.assertEqual(self.recording.external_storage_key, "meetings/test.mp3")
            self.assertIsNone(self.recording.local_file_path)
            self.assertEqual(self.recording.duration_ms, 12345)
            self.assertFalse(path.exists())
            self.assertEqual(
                WebhookDeliveryAttempt.objects.filter(
                    bot=self.bot,
                    webhook_trigger_type=WebhookTriggerTypes.RECORDING_READY,
                ).count(),
                1,
            )

            deliver_recording.run(self.recording.id)
            self.assertEqual(mock_primary.call_count, 1)
            self.assertEqual(mock_external.call_count, 1)
            self.assertEqual(
                WebhookDeliveryAttempt.objects.filter(
                    bot=self.bot,
                    webhook_trigger_type=WebhookTriggerTypes.RECORDING_READY,
                ).count(),
                1,
            )

    @patch("bots.tasks.recording_delivery_task.cleanup_ready_recording_spool", side_effect=RuntimeError("worker stopped before cleanup"))
    @patch("bots.tasks.recording_delivery_task._upload_external", return_value="meetings/test.mp3")
    @patch("bots.tasks.recording_delivery_task._upload_primary", return_value="primary/test.mp3")
    @patch("bots.tasks.recording_delivery_task._repair_mp3_if_needed", return_value=12345)
    def test_ready_delivery_keeps_durable_pointer_until_local_cleanup_succeeds(
        self,
        _mock_repair,
        _mock_primary,
        _mock_external,
        _mock_cleanup,
    ):
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "recording.mp3"
            path.write_bytes(b"complete meeting audio")
            self.recording.local_file_path = str(path)
            self.recording.delivery_state = RecordingDeliveryStates.STAGED
            self.recording.save(update_fields=["local_file_path", "delivery_state", "updated_at"])

            deliver_recording.run(self.recording.id)

            self.recording.refresh_from_db()
            self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.READY)
            self.assertEqual(self.recording.local_file_path, str(path))
            self.assertTrue(path.exists())

            self.assertTrue(cleanup_ready_recording_spool(self.recording.id))
            self.recording.refresh_from_db()
            self.assertIsNone(self.recording.local_file_path)
            self.assertFalse(path.exists())

    @patch("bots.tasks.recording_delivery_task._enqueue_task", return_value=False)
    def test_orphaned_spool_file_is_preserved_and_marked_partial(self, _mock_enqueue):
        with tempfile.TemporaryDirectory() as temp_directory, patch.dict("os.environ", {"BOT_RECORDING_SPOOL_DIRECTORY": temp_directory}):
            path = expected_spool_path(self.recording)
            path.write_bytes(b"partial meeting audio")

            self.assertTrue(recover_recording_from_spool(self.recording.id))

            self.recording.refresh_from_db()
            self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.STAGED)
            self.assertTrue(self.recording.is_partial)
            self.assertEqual(self.recording.local_file_path, str(path))
            self.assertTrue(path.exists())

    def test_missing_orphaned_spool_file_records_a_bounded_recovery_attempt(self):
        with tempfile.TemporaryDirectory() as temp_directory, patch.dict("os.environ", {"BOT_RECORDING_SPOOL_DIRECTORY": temp_directory}):
            self.assertFalse(recover_recording_from_spool(self.recording.id))

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.FAILED)
        self.assertEqual(self.recording.delivery_attempt_count, 1)
        self.assertEqual(self.recording.delivery_failure_data["error_type"], "RecordingSpoolUnavailable")

    def test_empty_expected_spool_is_deleted_and_not_retried(self):
        with tempfile.TemporaryDirectory() as temp_directory, patch.dict("os.environ", {"BOT_RECORDING_SPOOL_DIRECTORY": temp_directory}):
            path = expected_spool_path(self.recording)
            path.touch()
            self.recording.local_file_path = str(path)
            self.recording.save(update_fields=["local_file_path", "updated_at"])

            self.assertFalse(recover_recording_from_spool(self.recording.id))

            self.recording.refresh_from_db()
            self.assertFalse(path.exists())
            self.assertIsNone(self.recording.local_file_path)
            self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.FAILED)
            self.assertEqual(
                self.recording.delivery_attempt_count,
                MAX_RECORDING_DELIVERY_RETRIES,
            )
            self.assertEqual(self.recording.delivery_failure_data["error_type"], "RecordingSpoolEmpty")

    def test_empty_symlink_at_expected_spool_path_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as temp_directory, patch.dict("os.environ", {"BOT_RECORDING_SPOOL_DIRECTORY": temp_directory}):
            outside_path = Path(temp_directory).parent / f"outside-{self.recording.object_id}.mp3"
            outside_path.touch()
            expected_path = expected_spool_path(self.recording)
            expected_path.symlink_to(outside_path)
            try:
                self.assertFalse(recover_recording_from_spool(self.recording.id))

                self.recording.refresh_from_db()
                self.assertTrue(expected_path.is_symlink())
                self.assertTrue(outside_path.exists())
                self.assertEqual(self.recording.delivery_attempt_count, 1)
            finally:
                expected_path.unlink(missing_ok=True)
                outside_path.unlink(missing_ok=True)

    def test_empty_file_outside_expected_spool_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as temp_directory, patch.dict("os.environ", {"BOT_RECORDING_SPOOL_DIRECTORY": temp_directory}):
            outside_path = Path(temp_directory).parent / f"outside-{self.recording.object_id}.mp3"
            outside_path.touch()
            try:
                self.recording.local_file_path = str(outside_path)
                self.recording.save(update_fields=["local_file_path", "updated_at"])

                self.assertFalse(recover_recording_from_spool(self.recording.id))

                self.recording.refresh_from_db()
                self.assertTrue(outside_path.exists())
                self.assertEqual(self.recording.local_file_path, str(outside_path))
                self.assertEqual(self.recording.delivery_attempt_count, 1)
            finally:
                outside_path.unlink(missing_ok=True)

    def test_claim_locks_only_the_recording_row(self):
        self.recording.delivery_state = RecordingDeliveryStates.STAGED
        self.recording.save(update_fields=["delivery_state", "updated_at"])

        with CaptureQueriesContext(connection) as queries:
            claimed = _claim_delivery(self.recording.id)

        self.assertEqual(claimed.id, self.recording.id)
        locking_queries = [query["sql"] for query in queries.captured_queries if "FOR UPDATE" in query["sql"]]
        self.assertEqual(len(locking_queries), 1)
        locking_query = locking_queries[0]
        self.assertIn('FOR UPDATE OF "bots_recording"', locking_query)
        self.assertNotIn('FOR UPDATE OF "bots_bot"', locking_query)
        self.assertNotIn('FOR UPDATE OF "bots_project"', locking_query)

    @patch(
        "bots.tasks.recording_delivery_task._claim_delivery",
        side_effect=OperationalError("deadlock detected"),
    )
    def test_database_deadlock_while_claiming_delivery_retries_immediately(self, _mock_claim):
        self.recording.delivery_state = RecordingDeliveryStates.STAGED
        self.recording.save(update_fields=["delivery_state", "updated_at"])

        with patch.object(deliver_recording, "retry", side_effect=Retry()) as mock_retry:
            with self.assertRaises(Retry):
                deliver_recording.run(self.recording.id)

        mock_retry.assert_called_once()
        retry_kwargs = mock_retry.call_args.kwargs
        self.assertIsInstance(retry_kwargs["exc"], OperationalError)
        self.assertEqual(retry_kwargs["countdown"], 1)
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.STAGED)
        self.assertEqual(self.recording.delivery_attempt_count, 0)

    @patch("bots.tasks.recording_delivery_task._upload_primary", side_effect=RuntimeError("storage unavailable"))
    @patch("bots.tasks.recording_delivery_task._repair_mp3_if_needed", return_value=12345)
    def test_storage_unavailability_preserves_spool_and_retries(self, _mock_repair, _mock_primary):
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "recording.mp3"
            path.write_bytes(b"complete meeting audio")
            self.recording.local_file_path = str(path)
            self.recording.delivery_state = RecordingDeliveryStates.STAGED
            self.recording.save(update_fields=["local_file_path", "delivery_state", "updated_at"])

            with patch.object(deliver_recording, "retry", side_effect=Retry()) as mock_retry:
                with self.assertRaises(Retry):
                    deliver_recording.run(self.recording.id)

            mock_retry.assert_called_once()
            self.recording.refresh_from_db()
            self.assertTrue(path.exists())
            self.assertEqual(self.recording.local_file_path, str(path))
            self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.STAGED)
            self.assertEqual(self.recording.delivery_attempt_count, 1)
            self.assertEqual(
                WebhookDeliveryAttempt.objects.filter(
                    bot=self.bot,
                    webhook_trigger_type=WebhookTriggerTypes.RECORDING_READY,
                ).count(),
                0,
            )

    @patch("bots.management.commands.run_scheduler.docker.from_env")
    def test_scheduler_marks_missing_recording_container_fatal_even_after_delivery(self, mock_docker_from_env):
        self.bot.state = BotStates.JOINED_RECORDING
        self.bot.last_heartbeat_timestamp = int(timezone.now().timestamp()) - 700
        self.bot.save(update_fields=["state", "last_heartbeat_timestamp", "updated_at"])
        self.recording.state = RecordingStates.IN_PROGRESS
        self.recording.delivery_state = RecordingDeliveryStates.READY
        self.recording.save(update_fields=["state", "delivery_state", "updated_at"])
        mock_docker_from_env.return_value.containers.get.side_effect = docker.errors.NotFound("missing")

        Command()._mark_crashed_recording_only_bots(timezone.now())

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

    @patch("bots.management.commands.run_scheduler.enqueue_recording_delivery", return_value=True)
    def test_scheduler_requeues_staged_recording_when_publish_was_interrupted(self, mock_enqueue):
        self.recording.delivery_state = RecordingDeliveryStates.STAGED
        self.recording.delivery_enqueued_at = None
        self.recording.save(update_fields=["delivery_state", "delivery_enqueued_at", "updated_at"])

        Command()._run_pending_recording_deliveries()

        mock_enqueue.assert_called_once_with(self.recording.id)

    @patch("bots.management.commands.run_scheduler.enqueue_recording_delivery", return_value=True)
    def test_scheduler_requeues_stale_upload_after_worker_interruption(self, mock_enqueue):
        self.recording.delivery_state = RecordingDeliveryStates.UPLOADING
        self.recording.delivery_started_at = timezone.now() - timezone.timedelta(minutes=20)
        self.recording.save(update_fields=["delivery_state", "delivery_started_at", "updated_at"])

        Command()._run_pending_recording_deliveries()

        mock_enqueue.assert_called_once_with(self.recording.id)

    def test_scheduler_cleans_ready_recording_spool_after_worker_interruption(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "recording.mp3"
            path.write_bytes(b"delivered meeting audio")
            self.recording.delivery_state = RecordingDeliveryStates.READY
            self.recording.local_file_path = str(path)
            self.recording.save(update_fields=["delivery_state", "local_file_path", "updated_at"])

            Command()._run_pending_recording_deliveries()

            self.recording.refresh_from_db()
            self.assertIsNone(self.recording.local_file_path)
            self.assertFalse(path.exists())

    def test_cleanup_failure_preserves_durable_pointer_for_scheduler_retry(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            path = Path(temp_directory) / "recording.mp3"
            path.write_bytes(b"delivered meeting audio")
            self.recording.delivery_state = RecordingDeliveryStates.READY
            self.recording.local_file_path = str(path)
            self.recording.save(update_fields=["delivery_state", "local_file_path", "updated_at"])

            with patch.object(Path, "unlink", side_effect=PermissionError("read-only spool")):
                self.assertFalse(cleanup_ready_recording_spool(self.recording.id))

            self.recording.refresh_from_db()
            self.assertEqual(self.recording.local_file_path, str(path))
            self.assertTrue(path.exists())

    @override_settings(
        STORAGE_PROTOCOL="s3",
        AWS_RECORDING_PUBLIC_ENDPOINT_URL="https://media-qa.example.test",
        AWS_RECORDING_STORAGE_BUCKET_NAME="attendee-recordings",
        AWS_S3_ADDRESSING_STYLE="path",
        RECORDING_STORAGE_BACKEND={
            "OPTIONS": {
                "access_key": "test-access",
                "secret_key": "test-secret",
            }
        },
    )
    @patch("boto3.client")
    def test_recording_url_uses_public_endpoint_instead_of_internal_minio(self, mock_client):
        mock_client.return_value.generate_presigned_url.return_value = (
            "https://media-qa.example.test/attendee-recordings/primary/test.mp3?signed=1"
        )
        recording = SimpleNamespace(
            file=SimpleNamespace(name="primary/test.mp3"),
        )

        url = Recording.url.fget(recording)

        self.assertTrue(url.startswith("https://media-qa.example.test/"))
        self.assertEqual(
            mock_client.call_args.kwargs["endpoint_url"],
            "https://media-qa.example.test",
        )
