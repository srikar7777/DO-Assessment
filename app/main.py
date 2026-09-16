"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.logging import get_logger, setup_logging
from app.models import thumbnail as thumbnail_models
from app.routes.health import router as health_router
from app.routes.thumbnails import router as thumbnails_router

setup_logging()
log = get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create tables and log startup and shutdown events."""
    Base.metadata.create_all(bind=engine)
    log.info(
        "service_started",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        database=settings.database_log_label,
        storage_backend=settings.storage_backend,
        models=len(thumbnail_models.Base.metadata.tables),
    )
    yield
    log.info("service_stopped", service=settings.app_name, version=settings.app_version)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Image thumbnail generation service: upload, resize, retrieve.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(thumbnails_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return structured 422 responses for invalid input."""
    details = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    log.warning("validation_error", path=request.url.path, error_count=len(details))
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "message": "Request validation failed",
            "details": details,
        },
    )


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """Return a safe 500 response when a database operation fails."""
    log.error(
        "database_error",
        path=request.url.path,
        error_type=type(exc).__name__,
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "database_error",
            "message": "A database error occurred",
            "details": [],
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler that never leaks internal details to the caller."""
    log.error(
        "unhandled_error",
        path=request.url.path,
        error_type=type(exc).__name__,
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred",
            "details": [],
        },
    )


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    """Return basic service metadata and documentation links."""
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/health",
    }
