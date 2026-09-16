"""HTTP endpoints for uploading images and retrieving thumbnails."""

import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.logging import get_logger
from app.core.security import enforce_rate_limit, require_api_key
from app.models.schemas import (
    PRESET_DIMENSIONS,
    ImageDetailOut,
    ImageListResponse,
    ImageStatus,
    PresetInfo,
    PresetListResponse,
    PresetName,
    ResizeRequest,
    UploadResponse,
)
from app.services.storage import StorageError
from app.services.thumbnail_service import (
    ImageNotFoundError,
    InvalidImageError,
    PayloadTooLargeError,
    ThumbnailService,
    UploadedImage,
    to_image_detail,
    to_image_out,
)

router = APIRouter(prefix="/v1", tags=["thumbnails"])
log = get_logger(__name__)

_CONTENT_TYPE_BY_FORMAT = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def get_service(db: Session = Depends(get_db)) -> ThumbnailService:
    """Provide a ThumbnailService bound to the request-scoped session."""
    return ThumbnailService(db)


def _build_resize_request(
    presets: str,
    width: int | None,
    height: int | None,
    output_format: str | None,
    quality: int | None,
) -> ResizeRequest:
    """Parse form fields into a validated ResizeRequest."""
    preset_names = [item.strip() for item in presets.split(",") if item.strip()]
    payload = {
        "presets": preset_names,
        "width": width,
        "height": height,
        "output_format": output_format,
        "quality": quality,
    }
    try:
        return ResizeRequest.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


@router.post(
    "/images",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
async def upload_images(
    files: list[UploadFile] = File(..., description="One or more image files"),
    presets: str = Form("medium", description="Comma-separated presets"),
    width: int | None = Form(None, description="Custom max width in pixels"),
    height: int | None = Form(None, description="Custom max height in pixels"),
    output_format: str | None = Form(None, description="jpeg, png or webp"),
    quality: int | None = Form(None, description="Output quality from 1 to 100"),
    service: ThumbnailService = Depends(get_service),
) -> UploadResponse:
    """Upload one or more images and generate thumbnails for each requested preset."""
    options = _build_resize_request(presets, width, height, output_format, quality)

    uploads: list[UploadedImage] = []
    for upload in files:
        data = await upload.read()
        uploads.append(
            UploadedImage(
                filename=upload.filename or "upload",
                content_type=upload.content_type or "application/octet-stream",
                data=data,
            )
        )

    try:
        images, failed = await service.create_images(uploads, options)
    except PayloadTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except InvalidImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend unavailable",
        ) from exc

    if not images:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid images could be processed",
        )

    return UploadResponse(
        uploaded=len(images),
        failed=failed,
        images=[to_image_detail(image) for image in images],
    )


@router.get("/images", response_model=ImageListResponse)
def list_images(
    status_filter: ImageStatus | None = Query(None, alias="status"),
    content_type: str | None = Query(None, min_length=1, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service: ThumbnailService = Depends(get_service),
) -> ImageListResponse:
    """List uploaded images with optional filtering and pagination."""
    images, total = service.list_images(status_filter, content_type, limit, offset)
    return ImageListResponse(
        total=total,
        limit=limit,
        offset=offset,
        results=[to_image_out(image) for image in images],
    )


@router.get("/images/{image_id}", response_model=ImageDetailOut)
def get_image(
    image_id: uuid.UUID,
    service: ThumbnailService = Depends(get_service),
) -> ImageDetailOut:
    """Return metadata for one image together with all of its thumbnails."""
    try:
        image = service.get_image(image_id)
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return to_image_detail(image)


@router.get("/images/{image_id}/file")
async def download_image(
    image_id: uuid.UUID,
    service: ThumbnailService = Depends(get_service),
) -> Response:
    """Download the original bytes of an uploaded image."""
    try:
        image = service.get_image(image_id)
        payload = await service.read_bytes(image.storage_key)
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend unavailable",
        ) from exc

    return Response(
        content=payload,
        media_type=image.content_type,
        headers={"Content-Disposition": f'inline; filename="{image.original_filename}"'},
    )


@router.get("/thumbnails/{thumbnail_id}/file")
async def download_thumbnail(
    thumbnail_id: uuid.UUID,
    service: ThumbnailService = Depends(get_service),
) -> Response:
    """Download the bytes of a generated thumbnail."""
    try:
        thumbnail = service.get_thumbnail(thumbnail_id)
        payload = await service.read_bytes(thumbnail.storage_key)
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend unavailable",
        ) from exc

    media_type = _CONTENT_TYPE_BY_FORMAT.get(thumbnail.format, "application/octet-stream")
    filename = f"{thumbnail.preset}-{thumbnail.width}x{thumbnail.height}.{thumbnail.format}"
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.delete(
    "/images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)
def delete_image(
    image_id: uuid.UUID,
    service: ThumbnailService = Depends(get_service),
) -> Response:
    """Delete an image, its thumbnails and every stored object."""
    try:
        service.delete_image(image_id)
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/presets", response_model=PresetListResponse)
def list_presets() -> PresetListResponse:
    """List the named presets the service supports."""
    presets = [
        PresetInfo(
            name=name,
            max_width=dimensions[0],
            max_height=dimensions[1],
            description=f"Fits within {dimensions[0]}x{dimensions[1]} preserving aspect ratio",
        )
        for name, dimensions in PRESET_DIMENSIONS.items()
    ]
    presets.append(
        PresetInfo(
            name=PresetName.CUSTOM,
            max_width=None,
            max_height=None,
            description="Caller-supplied width and/or height, preserving aspect ratio",
        )
    )
    return PresetListResponse(presets=presets)


@router.get("/stats")
def get_stats(service: ThumbnailService = Depends(get_service)) -> dict[str, int]:
    """Return aggregate counts of stored images, thumbnails and bytes."""
    return service.get_stats()
