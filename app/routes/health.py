"""Liveness and readiness endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.logging import get_logger
from app.models.schemas import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])
log = get_logger(__name__)


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness probe: confirms the process is up without touching the database."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/ready", response_model=ReadinessResponse)
def ready(response: Response, db: Session = Depends(get_db)) -> ReadinessResponse:
    """Readiness probe: verifies the database answers a trivial query."""
    settings = get_settings()
    database_state = "ok"
    overall_status = "ready"

    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db.rollback()
        database_state = "unavailable"
        overall_status = "not_ready"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.error("readiness_check_failed", database=settings.database_log_label, exc_info=True)

    return ReadinessResponse(
        status=overall_status,
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        timestamp=datetime.now(timezone.utc).isoformat(),
        database=database_state,
    )
