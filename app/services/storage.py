"""Storage backends for original images and generated thumbnails.

Local disk is used for development and tests; an S3-compatible backend
(DigitalOcean Spaces) is used in production when STORAGE_BACKEND=s3.
"""

from pathlib import Path
from typing import Protocol

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


class StorageError(Exception):
    """Raised when reading from or writing to the storage backend fails."""


class StorageBackend(Protocol):
    """Minimal object-storage interface used by the thumbnail service."""

    def save(self, key: str, data: bytes, content_type: str) -> None:
        """Persist bytes under the given key."""
        ...

    def load(self, key: str) -> bytes:
        """Return the bytes stored under the given key."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object stored under the given key, if it exists."""
        ...

    def public_url(self, key: str) -> str | None:
        """Return a publicly reachable URL for the key, when the backend has one."""
        ...


class LocalStorage:
    """Filesystem-backed storage rooted at settings.storage_dir."""

    def __init__(self, root: str) -> None:
        """Create the storage root directory if it does not already exist."""
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """Resolve a storage key to an absolute path inside the storage root."""
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root):
            raise StorageError("storage key escapes the storage root")
        return candidate

    def save(self, key: str, data: bytes, content_type: str) -> None:
        """Write bytes to disk, creating parent directories as needed."""
        path = self._resolve(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            raise StorageError(f"failed to write {key}") from exc

    def load(self, key: str) -> bytes:
        """Read bytes from disk."""
        path = self._resolve(key)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"failed to read {key}") from exc

    def delete(self, key: str) -> None:
        """Delete a file from disk, ignoring a missing file."""
        path = self._resolve(key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"failed to delete {key}") from exc

    def public_url(self, key: str) -> str | None:
        """Local files are streamed through the API, so there is no public URL."""
        return None


class S3Storage:
    """S3-compatible storage (DigitalOcean Spaces, AWS S3)."""

    def __init__(self, settings: Settings) -> None:
        """Build an S3 client from the configured credentials and endpoint."""
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - only hit without boto3
            raise StorageError("boto3 is required for STORAGE_BACKEND=s3") from exc

        self._bucket = settings.s3_bucket
        self._public_base_url = settings.s3_public_base_url.rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
        )

    def save(self, key: str, data: bytes, content_type: str) -> None:
        """Upload bytes to the configured bucket."""
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except Exception as exc:
            raise StorageError(f"failed to upload {key}") from exc

    def load(self, key: str) -> bytes:
        """Download bytes from the configured bucket."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = response["Body"].read()
            return body
        except Exception as exc:
            raise StorageError(f"failed to download {key}") from exc

    def delete(self, key: str) -> None:
        """Delete an object from the configured bucket."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"failed to delete {key}") from exc

    def public_url(self, key: str) -> str | None:
        """Return a CDN/public URL when S3_PUBLIC_BASE_URL is configured."""
        if not self._public_base_url:
            return None
        return f"{self._public_base_url}/{key}"


_backend: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Return the process-wide storage backend selected by configuration."""
    global _backend
    if _backend is None:
        settings = get_settings()
        if settings.storage_backend == "s3":
            _backend = S3Storage(settings)
        else:
            _backend = LocalStorage(settings.storage_dir)
        log.info("storage_initialized", backend=settings.storage_backend)
    return _backend


def reset_storage() -> None:
    """Clear the cached backend so tests can point at a temporary directory."""
    global _backend
    _backend = None
