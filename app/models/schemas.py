"""Pydantic V2 schemas for request input and response output.

These are deliberately separate from the SQLAlchemy models so that callers can
never set server-generated fields (id, status, received_at, storage keys).
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DIMENSION = 4096
MIN_DIMENSION = 1


class PresetName(str, Enum):
    """Named thumbnail presets offered by the service."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    CUSTOM = "custom"


class OutputFormat(str, Enum):
    """Supported thumbnail output formats."""

    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"


class ImageStatus(str, Enum):
    """Lifecycle status of an uploaded image."""

    RECEIVED = "received"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class ThumbnailStatus(str, Enum):
    """Lifecycle status of a single generated thumbnail."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


PRESET_DIMENSIONS: dict[PresetName, tuple[int, int]] = {
    PresetName.SMALL: (150, 150),
    PresetName.MEDIUM: (400, 400),
    PresetName.LARGE: (1024, 1024),
}


class ResizeRequest(BaseModel):
    """Caller-supplied resize options for an upload request."""

    model_config = ConfigDict(extra="forbid")

    presets: list[PresetName] = Field(default_factory=lambda: [PresetName.MEDIUM])
    width: Annotated[int, Field(ge=MIN_DIMENSION, le=MAX_DIMENSION)] | None = None
    height: Annotated[int, Field(ge=MIN_DIMENSION, le=MAX_DIMENSION)] | None = None
    output_format: OutputFormat | None = None
    quality: Annotated[int, Field(ge=1, le=100)] | None = None

    @field_validator("presets")
    @classmethod
    def validate_presets(cls, value: list[PresetName]) -> list[PresetName]:
        """Reject an empty preset list and de-duplicate while preserving order."""
        if not value:
            raise ValueError("presets must contain at least one entry")
        deduped: list[PresetName] = []
        for preset in value:
            if preset not in deduped:
                deduped.append(preset)
        return deduped

    @model_validator(mode="after")
    def validate_custom_dimensions(self) -> "ResizeRequest":
        """Require width or height for the custom preset, and forbid them otherwise."""
        has_custom = PresetName.CUSTOM in self.presets
        has_dimension = self.width is not None or self.height is not None
        if has_custom and not has_dimension:
            raise ValueError("custom preset requires width and/or height")
        if has_dimension and not has_custom:
            raise ValueError("width/height are only allowed with the custom preset")
        return self


class ThumbnailOut(BaseModel):
    """Metadata describing one generated thumbnail."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    image_id: uuid.UUID
    preset: str
    width: int
    height: int
    format: str
    size_bytes: int
    quality: int
    status: ThumbnailStatus
    created_at: datetime
    download_url: str


class ImageOut(BaseModel):
    """Metadata describing one uploaded image."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    width: int
    height: int
    status: ImageStatus
    error_message: str | None = None
    received_at: datetime
    updated_at: datetime
    download_url: str


class ImageDetailOut(ImageOut):
    """An uploaded image together with all of its thumbnails."""

    thumbnails: list[ThumbnailOut] = Field(default_factory=list)


class UploadResponse(BaseModel):
    """Result of a batch upload + resize request."""

    uploaded: int = Field(ge=0)
    failed: int = Field(ge=0)
    images: list[ImageDetailOut] = Field(default_factory=list)


class ImageListResponse(BaseModel):
    """Paginated list of images."""

    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    results: list[ImageOut] = Field(default_factory=list)


class PresetInfo(BaseModel):
    """Public description of a named preset."""

    name: PresetName
    max_width: int | None = None
    max_height: int | None = None
    description: str = Field(min_length=1, max_length=255)


class PresetListResponse(BaseModel):
    """All presets the service supports."""

    presets: list[PresetInfo] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Liveness payload."""

    status: str = Field(min_length=1, max_length=32)
    service: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=32)
    environment: str = Field(min_length=1, max_length=32)
    timestamp: str = Field(min_length=1, max_length=64)


class ReadinessResponse(HealthResponse):
    """Readiness payload including dependency checks."""

    database: str = Field(min_length=1, max_length=32)


class ErrorDetail(BaseModel):
    """A single field-level validation error."""

    field: str
    message: str
    type: str


class ErrorResponse(BaseModel):
    """Structured error envelope returned by exception handlers."""

    error: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    details: list[ErrorDetail] = Field(default_factory=list)
