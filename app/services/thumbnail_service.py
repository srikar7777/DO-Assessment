"""Business logic for uploading images and generating thumbnails."""

import asyncio
import io
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import anyio.to_thread
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schemas import (
    PRESET_DIMENSIONS,
    ImageDetailOut,
    ImageOut,
    ImageStatus,
    OutputFormat,
    PresetName,
    ResizeRequest,
    ThumbnailOut,
    ThumbnailStatus,
)
from app.models.thumbnail import Image as ImageModel
from app.models.thumbnail import Thumbnail as ThumbnailModel
from app.services.storage import StorageError, get_storage

log = get_logger(__name__)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
}

_FORMAT_EXTENSIONS: dict[OutputFormat, str] = {
    OutputFormat.JPEG: "jpg",
    OutputFormat.PNG: "png",
    OutputFormat.WEBP: "webp",
}

_FORMAT_CONTENT_TYPES: dict[OutputFormat, str] = {
    OutputFormat.JPEG: "image/jpeg",
    OutputFormat.PNG: "image/png",
    OutputFormat.WEBP: "image/webp",
}


class ThumbnailServiceError(Exception):
    """Base error for the thumbnail service."""


class ImageNotFoundError(ThumbnailServiceError):
    """Raised when a requested image or thumbnail does not exist."""


class InvalidImageError(ThumbnailServiceError):
    """Raised when an upload is not a usable image."""


class PayloadTooLargeError(ThumbnailServiceError):
    """Raised when an upload exceeds the configured size or count limits."""


@dataclass(frozen=True)
class UploadedImage:
    """Transport-agnostic representation of one uploaded file."""

    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class RenderedThumbnail:
    """A resized image produced in a worker thread, before persistence."""

    preset: PresetName
    width: int
    height: int
    output_format: OutputFormat
    quality: int
    data: bytes


def utc_now() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def _target_box(
    preset: PresetName,
    source_width: int,
    source_height: int,
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    """Return the bounding box to fit the image into for a preset."""
    if preset is not PresetName.CUSTOM:
        return PRESET_DIMENSIONS[preset]
    box_width = width or source_width
    box_height = height or source_height
    return box_width, box_height


def _render_thumbnail(
    source_bytes: bytes,
    preset: PresetName,
    options: ResizeRequest,
    default_quality: int,
) -> RenderedThumbnail:
    """Resize image bytes into one thumbnail, preserving the aspect ratio."""
    with PILImage.open(io.BytesIO(source_bytes)) as source:
        oriented = ImageOps.exif_transpose(source) or source
        box = _target_box(
            preset,
            oriented.width,
            oriented.height,
            options.width,
            options.height,
        )
        resized = oriented.copy()
        # Image.thumbnail preserves the aspect ratio and never upscales.
        resized.thumbnail(box, PILImage.Resampling.LANCZOS)

        output_format = options.output_format or _default_format(source.format)
        if output_format is OutputFormat.JPEG and resized.mode not in ("RGB", "L"):
            resized = resized.convert("RGB")

        quality = options.quality or default_quality
        buffer = io.BytesIO()
        save_kwargs: dict[str, object] = {}
        if output_format in (OutputFormat.JPEG, OutputFormat.WEBP):
            save_kwargs["quality"] = quality
        if output_format is OutputFormat.JPEG:
            save_kwargs["optimize"] = True
        resized.save(buffer, format=output_format.value.upper(), **save_kwargs)

        return RenderedThumbnail(
            preset=preset,
            width=resized.width,
            height=resized.height,
            output_format=output_format,
            quality=quality,
            data=buffer.getvalue(),
        )


def _default_format(source_format: str | None) -> OutputFormat:
    """Pick an output format matching the source, defaulting to JPEG."""
    normalized = (source_format or "").lower()
    if normalized == "png":
        return OutputFormat.PNG
    if normalized == "webp":
        return OutputFormat.WEBP
    return OutputFormat.JPEG


def _probe_dimensions(data: bytes) -> tuple[int, int, str]:
    """Return (width, height, format) for image bytes, or raise InvalidImageError."""
    try:
        with PILImage.open(io.BytesIO(data)) as probe:
            probe.verify()
        with PILImage.open(io.BytesIO(data)) as probe:
            oriented = ImageOps.exif_transpose(probe) or probe
            return oriented.width, oriented.height, (probe.format or "unknown").lower()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("file is not a readable image") from exc


class ThumbnailService:
    """Owns all image-processing and database work for the thumbnail domain."""

    def __init__(self, db: Session) -> None:
        """Bind the service to a request-scoped session and the storage backend."""
        self._db = db
        self._settings = get_settings()
        self._storage = get_storage()

    async def create_images(
        self,
        uploads: list[UploadedImage],
        options: ResizeRequest,
    ) -> tuple[list[ImageModel], int]:
        """Validate, resize and persist a batch of uploads concurrently."""
        if not uploads:
            raise InvalidImageError("at least one file is required")
        if len(uploads) > self._settings.max_images_per_request:
            raise PayloadTooLargeError(
                f"at most {self._settings.max_images_per_request} files per request"
            )

        results = await asyncio.gather(
            *(self._process_one(upload, options) for upload in uploads),
            return_exceptions=True,
        )

        created: list[ImageModel] = []
        failed = 0
        for upload, result in zip(uploads, results, strict=True):
            if isinstance(result, BaseException):
                failed += 1
                log.warning(
                    "image_processing_failed",
                    filename=upload.filename,
                    error=str(result),
                )
                continue
            created.append(result)

        if created:
            self._commit(created)

        log.info("upload_batch_completed", uploaded=len(created), failed=failed)
        return created, failed

    async def _process_one(
        self,
        upload: UploadedImage,
        options: ResizeRequest,
    ) -> ImageModel:
        """Validate one upload and render all requested thumbnails off the event loop."""
        self._validate_upload(upload)
        width, height, _ = await anyio.to_thread.run_sync(_probe_dimensions, upload.data)

        image_id = uuid.uuid4()
        original_key = f"originals/{image_id}/{_safe_name(upload.filename)}"
        await anyio.to_thread.run_sync(
            self._storage.save,
            original_key,
            upload.data,
            upload.content_type,
        )

        image = ImageModel(
            id=image_id,
            original_filename=upload.filename,
            content_type=upload.content_type,
            size_bytes=len(upload.data),
            width=width,
            height=height,
            storage_key=original_key,
            status=ImageStatus.READY.value,
            received_at=utc_now(),
            updated_at=utc_now(),
        )

        rendered = await asyncio.gather(
            *(
                anyio.to_thread.run_sync(
                    _render_thumbnail,
                    upload.data,
                    preset,
                    options,
                    self._settings.default_thumbnail_quality,
                )
                for preset in options.presets
            )
        )

        for item in rendered:
            thumbnail_id = uuid.uuid4()
            extension = _FORMAT_EXTENSIONS[item.output_format]
            key = f"thumbnails/{image_id}/{thumbnail_id}.{extension}"
            await anyio.to_thread.run_sync(
                self._storage.save,
                key,
                item.data,
                _FORMAT_CONTENT_TYPES[item.output_format],
            )
            image.thumbnails.append(
                ThumbnailModel(
                    id=thumbnail_id,
                    image_id=image_id,
                    preset=item.preset.value,
                    width=item.width,
                    height=item.height,
                    format=item.output_format.value,
                    size_bytes=len(item.data),
                    storage_key=key,
                    quality=item.quality,
                    status=ThumbnailStatus.READY.value,
                    created_at=utc_now(),
                )
            )

        return image

    def _validate_upload(self, upload: UploadedImage) -> None:
        """Enforce content-type and size limits before any decoding happens."""
        if not upload.filename.strip():
            raise InvalidImageError("filename must not be blank")
        if upload.content_type.lower() not in ALLOWED_CONTENT_TYPES:
            raise InvalidImageError(f"unsupported content type: {upload.content_type}")
        if not upload.data:
            raise InvalidImageError("uploaded file is empty")
        if len(upload.data) > self._settings.max_upload_bytes:
            raise PayloadTooLargeError(
                f"file exceeds the {self._settings.max_upload_bytes} byte limit"
            )

    def _commit(self, images: list[ImageModel]) -> None:
        """Persist images and refresh them to pick up server-generated values."""
        try:
            self._db.add_all(images)
            self._db.commit()
            for image in images:
                self._db.refresh(image)
        except SQLAlchemyError:
            self._db.rollback()
            log.error("image_persist_failed", count=len(images), exc_info=True)
            raise

    def get_image(self, image_id: uuid.UUID) -> ImageModel:
        """Return one image with its thumbnails, or raise ImageNotFoundError."""
        try:
            stmt = (
                select(ImageModel)
                .options(selectinload(ImageModel.thumbnails))
                .where(ImageModel.id == image_id)
            )
            image = self._db.execute(stmt).scalar_one_or_none()
        except SQLAlchemyError:
            self._db.rollback()
            log.error("image_lookup_failed", image_id=str(image_id), exc_info=True)
            raise
        if image is None:
            raise ImageNotFoundError(f"image {image_id} not found")
        return image

    def list_images(
        self,
        status: ImageStatus | None,
        content_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ImageModel], int]:
        """Return a page of images plus the total count matching the filters."""
        try:
            filters = []
            if status is not None:
                filters.append(ImageModel.status == status.value)
            if content_type is not None:
                filters.append(ImageModel.content_type == content_type)

            count_stmt = select(func.count()).select_from(ImageModel)
            page_stmt = select(ImageModel)
            for condition in filters:
                count_stmt = count_stmt.where(condition)
                page_stmt = page_stmt.where(condition)

            total = self._db.execute(count_stmt).scalar_one()
            page_stmt = (
                page_stmt.order_by(ImageModel.received_at.desc()).limit(limit).offset(offset)
            )
            images = list(self._db.execute(page_stmt).scalars().all())
        except SQLAlchemyError:
            self._db.rollback()
            log.error("image_list_failed", exc_info=True)
            raise
        return images, total

    def get_thumbnail(self, thumbnail_id: uuid.UUID) -> ThumbnailModel:
        """Return one thumbnail, or raise ImageNotFoundError."""
        try:
            stmt = select(ThumbnailModel).where(ThumbnailModel.id == thumbnail_id)
            thumbnail = self._db.execute(stmt).scalar_one_or_none()
        except SQLAlchemyError:
            self._db.rollback()
            log.error("thumbnail_lookup_failed", thumbnail_id=str(thumbnail_id), exc_info=True)
            raise
        if thumbnail is None:
            raise ImageNotFoundError(f"thumbnail {thumbnail_id} not found")
        return thumbnail

    async def read_bytes(self, storage_key: str) -> bytes:
        """Load stored bytes without blocking the event loop."""
        try:
            return await anyio.to_thread.run_sync(self._storage.load, storage_key)
        except StorageError:
            log.error("storage_read_failed", key=storage_key, exc_info=True)
            raise

    def delete_image(self, image_id: uuid.UUID) -> None:
        """Delete an image, its thumbnails and all stored objects."""
        image = self.get_image(image_id)
        keys = [image.storage_key] + [t.storage_key for t in image.thumbnails]
        try:
            self._db.delete(image)
            self._db.commit()
        except SQLAlchemyError:
            self._db.rollback()
            log.error("image_delete_failed", image_id=str(image_id), exc_info=True)
            raise
        for key in keys:
            try:
                self._storage.delete(key)
            except StorageError:
                log.warning("storage_delete_failed", key=key)
        log.info("image_deleted", image_id=str(image_id), objects=len(keys))

    def get_stats(self) -> dict[str, int]:
        """Return aggregate usage counters for operators and customers."""
        try:
            images = self._db.execute(select(func.count()).select_from(ImageModel)).scalar_one()
            thumbnails = self._db.execute(
                select(func.count()).select_from(ThumbnailModel)
            ).scalar_one()
            original_bytes = (
                self._db.execute(select(func.coalesce(func.sum(ImageModel.size_bytes), 0)))
            ).scalar_one()
            thumbnail_bytes = (
                self._db.execute(select(func.coalesce(func.sum(ThumbnailModel.size_bytes), 0)))
            ).scalar_one()
        except SQLAlchemyError:
            self._db.rollback()
            log.error("stats_failed", exc_info=True)
            raise
        return {
            "images": int(images),
            "thumbnails": int(thumbnails),
            "original_bytes": int(original_bytes),
            "thumbnail_bytes": int(thumbnail_bytes),
        }


def _safe_name(filename: str) -> str:
    """Strip directory components from an uploaded filename."""
    return filename.replace("\\", "/").split("/")[-1][:255] or "upload"


def _as_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime.

    SQLite does not persist timezone offsets, so values read back from a local
    database are naive even though they were stored as UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_thumbnail_out(thumbnail: ThumbnailModel) -> ThumbnailOut:
    """Map a thumbnail row to its API representation."""
    storage = get_storage()
    return ThumbnailOut(
        id=thumbnail.id,
        image_id=thumbnail.image_id,
        preset=thumbnail.preset,
        width=thumbnail.width,
        height=thumbnail.height,
        format=thumbnail.format,
        size_bytes=thumbnail.size_bytes,
        quality=thumbnail.quality,
        status=ThumbnailStatus(thumbnail.status),
        created_at=_as_utc(thumbnail.created_at),
        download_url=storage.public_url(thumbnail.storage_key)
        or f"/v1/thumbnails/{thumbnail.id}/file",
    )


def to_image_out(image: ImageModel) -> ImageOut:
    """Map an image row to its API representation."""
    storage = get_storage()
    return ImageOut(
        id=image.id,
        original_filename=image.original_filename,
        content_type=image.content_type,
        size_bytes=image.size_bytes,
        width=image.width,
        height=image.height,
        status=ImageStatus(image.status),
        error_message=image.error_message,
        received_at=_as_utc(image.received_at),
        updated_at=_as_utc(image.updated_at),
        download_url=storage.public_url(image.storage_key) or f"/v1/images/{image.id}/file",
    )


def to_image_detail(image: ImageModel) -> ImageDetailOut:
    """Map an image row and its thumbnails to the detailed API representation."""
    base = to_image_out(image)
    return ImageDetailOut(
        **base.model_dump(),
        thumbnails=[to_thumbnail_out(t) for t in image.thumbnails],
    )
