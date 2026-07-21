import importlib.util
import tempfile
from pathlib import Path
from unittest import TestCase, mock

_MODULE_PATH = Path(__file__).parents[1] / "bot_controller" / "s3_file_uploader.py"
_SPEC = importlib.util.spec_from_file_location("s3_file_uploader_reliability", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_UPLOADER_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_UPLOADER_MODULE)
S3FileUploader = _UPLOADER_MODULE.S3FileUploader
UploadFailed = _UPLOADER_MODULE.UploadFailed


class S3FileUploaderReliabilityTest(TestCase):
    @mock.patch.object(_UPLOADER_MODULE.boto3, "client")
    def test_retries_and_verifies_external_upload(self, mock_client_factory):
        client = mock_client_factory.return_value
        client.upload_file.side_effect = [RuntimeError("temporary"), None]
        client.head_object.return_value = {"ContentLength": 3}

        with tempfile.NamedTemporaryFile() as source:
            source.write(b"abc")
            source.flush()
            uploader = S3FileUploader(
                bucket="bucket",
                filename="recording.mp3",
                max_attempts=2,
                initial_retry_delay_seconds=0,
                verify_upload=True,
            )
            uploader.upload_file(source.name)
            uploader.wait_for_upload(raise_on_error=True)

        self.assertEqual(client.upload_file.call_count, 2)
        client.head_object.assert_called_once_with(Bucket="bucket", Key="recording.mp3")

    @mock.patch.object(_UPLOADER_MODULE.boto3, "client")
    def test_surfaces_failure_when_requested(self, mock_client_factory):
        client = mock_client_factory.return_value
        client.upload_file.side_effect = RuntimeError("unavailable")

        with tempfile.NamedTemporaryFile() as source:
            source.write(b"abc")
            source.flush()
            uploader = S3FileUploader(
                bucket="bucket",
                filename="recording.mp3",
                max_attempts=1,
                initial_retry_delay_seconds=0,
            )
            uploader.upload_file(source.name)
            with self.assertRaises(UploadFailed):
                uploader.wait_for_upload(raise_on_error=True)
