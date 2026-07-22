import tempfile
from pathlib import Path
from unittest.mock import patch

import docker
from django.test import TestCase
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
from bots.tasks.recording_delivery_task import cleanup_ready_recording_spool, deliver_recording, expected_spool_path, recover_recording_from_spool


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
        with tempfile.TemporaryDirectory() as temp_directory, patch.dict(
            "os.environ", {"BOT_RECORDING_SPOOL_DIRECTORY": temp_directory}
        ):
            path = expected_spool_path(self.recording)
            path.write_bytes(b"partial meeting audio")

            self.assertTrue(recover_recording_from_spool(self.recording.id))

            self.recording.refresh_from_db()
            self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.STAGED)
            self.assertTrue(self.recording.is_partial)
            self.assertEqual(self.recording.local_file_path, str(path))
            self.assertTrue(path.exists())

    def test_missing_orphaned_spool_file_records_a_bounded_recovery_attempt(self):
        with tempfile.TemporaryDirectory() as temp_directory, patch.dict(
            "os.environ", {"BOT_RECORDING_SPOOL_DIRECTORY": temp_directory}
        ):
            self.assertFalse(recover_recording_from_spool(self.recording.id))

        self.recording.refresh_from_db()
        self.assertEqual(self.recording.delivery_state, RecordingDeliveryStates.FAILED)
        self.assertEqual(self.recording.delivery_attempt_count, 1)
        self.assertEqual(self.recording.delivery_failure_data["error_type"], "RecordingSpoolUnavailable")

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
