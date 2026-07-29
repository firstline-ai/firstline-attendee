import logging
import threading
import time
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class UploadFailed(RuntimeError):
    """Raised when an upload did not finish successfully."""


class S3FileUploader:
    def __init__(
        self,
        bucket,
        filename,
        endpoint_url=None,
        region_name=None,
        access_key_id=None,
        access_key_secret=None,
        *,
        max_attempts=1,
        initial_retry_delay_seconds=1,
        verify_upload=False,
    ):
        """Initialize the S3FileUploader with an S3 bucket name.

        Args:
            bucket (str): The name of the S3 bucket to upload to
            filename (str): The name of the to be stored file
        """
        self.s3_client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region_name, aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret)
        self.bucket = bucket
        self.filename = filename
        self._upload_thread = None
        self._upload_error = None
        self.max_attempts = max(1, int(max_attempts))
        self.initial_retry_delay_seconds = max(0, float(initial_retry_delay_seconds))
        self.verify_upload = verify_upload

    def upload_file(self, file_path: str, callback=None):
        """Start an asynchronous upload of a file to S3.

        Args:
            file_path (str): Path to the local file to upload
            callback (callable, optional): Function to call when upload completes
        """
        self._upload_thread = threading.Thread(target=self._upload_worker, args=(file_path, callback), daemon=True)
        self._upload_thread.start()

    def _upload_worker(self, file_path: str, callback=None):
        """Background thread that handles the actual file upload.

        Args:
            file_path (str): Path to the local file to upload
            callback (callable, optional): Function to call when upload completes
        """
        file_path = Path(file_path)
        if not file_path.exists():
            self._upload_error = FileNotFoundError(f"File not found: {file_path}")
            if callback:
                callback(False)
            return

        for attempt in range(1, self.max_attempts + 1):
            try:
                self.s3_client.upload_file(str(file_path), self.bucket, self.filename)
                if self.verify_upload:
                    remote = self.s3_client.head_object(Bucket=self.bucket, Key=self.filename)
                    remote_size = remote.get("ContentLength")
                    local_size = file_path.stat().st_size
                    if remote_size != local_size:
                        raise UploadFailed(
                            f"Remote size mismatch for s3://{self.bucket}/{self.filename}: expected {local_size}, got {remote_size}"
                        )

                self._upload_error = None
                logger.info(f"Successfully uploaded {file_path} to s3://{self.bucket}/{self.filename}")
                if callback:
                    callback(True)
                return
            except Exception as exc:
                self._upload_error = exc
                if attempt == self.max_attempts:
                    logger.error(
                        "Upload failed after %s attempt(s) for s3://%s/%s: %s",
                        attempt,
                        self.bucket,
                        self.filename,
                        exc,
                    )
                    if callback:
                        callback(False)
                    return
                delay = self.initial_retry_delay_seconds * (2 ** (attempt - 1))
                logger.warning(
                    "Upload attempt %s/%s failed for s3://%s/%s; retrying in %.1fs: %s",
                    attempt,
                    self.max_attempts,
                    self.bucket,
                    self.filename,
                    delay,
                    exc,
                )
                time.sleep(delay)

    def wait_for_upload(self, *, raise_on_error=False):
        """Wait for the current upload and optionally surface its error."""
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()
        if raise_on_error and self._upload_error:
            raise UploadFailed(f"Upload failed for s3://{self.bucket}/{self.filename}") from self._upload_error

    def delete_file(self, file_path: str):
        """Delete a file from the local filesystem."""
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
