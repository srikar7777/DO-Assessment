"""HTTP route modules."""

from app.routes.health import router as health_router
from app.routes.thumbnails import router as thumbnails_router

__all__ = ["health_router", "thumbnails_router"]
