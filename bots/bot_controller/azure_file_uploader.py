import logging
import threading
import time
from pathlib import Path

from azure.storage.blob import BlobClient, BlobServiceClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class UploadFailed(RuntimeError):
    """Raised when an Azure upload did not finish successfully."""


class AzureFileUploader:
    def __init__(
        self,
        container,
        filename,
        connection_string,
        account_key,
        account_name,
        *,
        max_attempts=1,
        initial_retry_delay_seconds=1,
        verify_upload=False,
    ):
        """
        Initialize the AzureFileUploader with a target container and blob name.

        Args:
            container (str): Azure Blob Storage container name.
            filename (str): Target blob name (path/key) inside the container.
            connection_string (str, optional): Full Azure Storage connection string.
            account_key (str, optional): Account key (used if no connection string).
            account_name (str, optional): Account name (used if no connection string).
        """
        if not container or not filename:
            raise ValueError("Both 'container' and 'filename' are required")

        # Prefer connection string if provided; otherwise fall back to account_name + account_key
        if connection_string:
            service_client = BlobServiceClient.from_connection_string(connection_string)
        elif account_name and account_key:
            account_url = f"https://{account_name}.blob.core.windows.net"
            service_client = BlobServiceClient(account_url=account_url, credential=account_key)
        else:
            raise ValueError("Provide either connection string or both account_name and account_key")

        # Keep a BlobClient ready to use (mirrors S3 "bucket/key" pairing)
        self.container = container
        self.filename = filename
        self.blob_client: BlobClient = service_client.get_blob_client(container=container, blob=filename)

        self._upload_thread = None
        self._upload_error = None
        self.max_attempts = max(1, int(max_attempts))
        self.initial_retry_delay_seconds = max(0, float(initial_retry_delay_seconds))
        self.verify_upload = verify_upload

    def upload_file(self, file_path: str, callback=None):
        """Start an asynchronous upload of a file to Azure Blob Storage.

        Args:
            file_path (str): Path to the local file to upload.
            callback (callable, optional): Function to call when upload completes; receives True/False.
        """
        self._upload_thread = threading.Thread(target=self._upload_worker, args=(file_path, callback), daemon=True)
        self._upload_thread.start()

    def _upload_worker(self, file_path: str, callback=None):
        """Background thread that handles the actual file upload."""
        file_path = Path(file_path)
        if not file_path.exists():
            self._upload_error = FileNotFoundError(f"File not found: {file_path}")
            if callback:
                callback(False)
            return

        for attempt in range(1, self.max_attempts + 1):
            try:
                with file_path.open("rb") as f:
                    self.blob_client.upload_blob(f, overwrite=True)
                if self.verify_upload:
                    remote_size = self.blob_client.get_blob_properties().size
                    local_size = file_path.stat().st_size
                    if remote_size != local_size:
                        raise UploadFailed(
                            f"Remote size mismatch for Azure blob {self.container}/{self.filename}: expected {local_size}, got {remote_size}"
                        )

                self._upload_error = None
                account_url = self.blob_client.url.split(f"/{self.container}/")[0]
                logger.info(f"Successfully uploaded {file_path} to {account_url}/{self.container}/{self.filename}")
                if callback:
                    callback(True)
                return
            except Exception as exc:
                self._upload_error = exc
                if attempt == self.max_attempts:
                    logger.error("Azure upload failed after %s attempt(s): %s", attempt, exc)
                    if callback:
                        callback(False)
                    return
                delay = self.initial_retry_delay_seconds * (2 ** (attempt - 1))
                logger.warning("Azure upload attempt %s/%s failed; retrying in %.1fs", attempt, self.max_attempts, delay)
                time.sleep(delay)

    def wait_for_upload(self, *, raise_on_error=False):
        """Wait for the current upload and optionally surface its error."""
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()
        if raise_on_error and self._upload_error:
            raise UploadFailed(f"Upload failed for Azure blob {self.container}/{self.filename}") from self._upload_error

    def delete_file(self, file_path: str):
        """Delete a file from the local filesystem (same behavior as the S3 version)."""
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
