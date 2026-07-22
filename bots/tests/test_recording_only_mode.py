import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from bots.bot_controller.s3_file_uploader import S3FileUploader, UploadFailed
from bots.models import TranscriptionTypes
from bots.serializers import CreateAsyncTranscriptionSerializer, CreateBotSerializer
from bots.tasks.recording_delivery_task import expected_spool_path
from bots.utils import transcription_provider_from_bot_creation_data, transcription_type_from_bot_creation_data


class RecordingOnlyContractTests(SimpleTestCase):
    def test_google_meet_mp3_accepts_recording_only_mode(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Recorder",
                "recording_settings": {"format": "mp3"},
                "transcription_settings": {"none": {}},
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNone(transcription_provider_from_bot_creation_data(serializer.validated_data))
        self.assertEqual(transcription_type_from_bot_creation_data(serializer.validated_data), TranscriptionTypes.NO_TRANSCRIPTION)

    def test_recording_only_mode_requires_mp3(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Recorder",
                "recording_settings": {"format": "mp4"},
                "transcription_settings": {"none": {}},
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("transcription_settings", serializer.errors)

    def test_recording_only_mode_cannot_be_combined_with_provider(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Recorder",
                "recording_settings": {"format": "mp3"},
                "transcription_settings": {"none": {}, "assembly_ai": {}},
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("transcription_settings", serializer.errors)

    def test_none_is_not_an_async_transcription_provider(self):
        serializer = CreateAsyncTranscriptionSerializer(data={"transcription_settings": {"none": {}}})

        self.assertFalse(serializer.is_valid())
        self.assertIn("transcription_settings", serializer.errors)

    @patch.dict("os.environ", {"BOT_RECORDING_SPOOL_DIRECTORY": "/durable-spool"})
    def test_spool_filename_is_deterministic(self):
        recording = SimpleNamespace(
            object_id="rec_123",
            bot=SimpleNamespace(object_id="bot_456", recording_format=lambda: "mp3"),
        )

        self.assertEqual(expected_spool_path(recording), Path("/durable-spool/bot_456-rec_123.mp3"))


class ReliableS3UploadTests(SimpleTestCase):
    @patch("bots.bot_controller.s3_file_uploader.time.sleep", return_value=None)
    @patch("bots.bot_controller.s3_file_uploader.boto3.client")
    def test_retries_and_verifies_remote_size(self, mock_boto_client, _mock_sleep):
        client = MagicMock()
        client.upload_file.side_effect = [RuntimeError("temporary"), None]
        mock_boto_client.return_value = client

        with tempfile.NamedTemporaryFile() as source:
            source.write(b"audio")
            source.flush()
            client.head_object.return_value = {"ContentLength": 5}
            uploader = S3FileUploader(
                bucket="recordings",
                filename="meeting.mp3",
                max_attempts=2,
                initial_retry_delay_seconds=0,
                verify_upload=True,
            )
            uploader.upload_file(source.name)
            uploader.wait_for_upload(raise_on_error=True)

        self.assertEqual(client.upload_file.call_count, 2)
        client.head_object.assert_called_once_with(Bucket="recordings", Key="meeting.mp3")

    @patch("bots.bot_controller.s3_file_uploader.boto3.client")
    def test_size_mismatch_is_a_failed_upload(self, mock_boto_client):
        client = MagicMock()
        client.head_object.return_value = {"ContentLength": 1}
        mock_boto_client.return_value = client

        with tempfile.NamedTemporaryFile() as source:
            source.write(b"audio")
            source.flush()
            uploader = S3FileUploader(
                bucket="recordings",
                filename="meeting.mp3",
                verify_upload=True,
            )
            uploader.upload_file(source.name)
            with self.assertRaises(UploadFailed):
                uploader.wait_for_upload(raise_on_error=True)
