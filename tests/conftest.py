"""Shared pytest fixtures.

Environment variables are set before any application module is imported so the
service binds to a dedicated test database and a throwaway storage directory.
"""

import io
import os
import shutil
from collections.abc import Generator, Iterator
from pathlib import Path

TEST_DIR = Path(__file__).parent
TEST_DB_PATH = TEST_DIR / "test_myservice.db"
TEST_STORAGE_DIR = TEST_DIR / "test_storage"

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["STORAGE_DIR"] = str(TEST_STORAGE_DIR)
os.environ["ENVIRONMENT"] = "development"
os.environ["APP_NAME"] = "myservice-test"
os.environ["APP_VERSION"] = "0.0.0-test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["API_KEY"] = ""
os.environ["RATE_LIMIT_REQUESTS_PER_MINUTE"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image as PILImage  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.core.security import reset_rate_limiter  # noqa: E402
from app.main import app  # noqa: E402


def override_get_db() -> Generator[Session, None, None]:
    """Yield a session bound to the test database."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    """Create tables before each test and drop them afterwards."""
    Base.metadata.create_all(bind=engine)
    reset_rate_limiter()
    yield
    Base.metadata.drop_all(bind=engine)
    if TEST_STORAGE_DIR.exists():
        shutil.rmtree(TEST_STORAGE_DIR, ignore_errors=True)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Return a TestClient with the database dependency overridden."""
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def make_image_bytes(
    width: int = 1200,
    height: int = 800,
    image_format: str = "JPEG",
    color: str = "red",
) -> bytes:
    """Generate in-memory image bytes for upload tests."""
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), color=color).save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.fixture
def sample_image() -> bytes:
    """Return bytes for a valid landscape JPEG."""
    return make_image_bytes()


@pytest.fixture
def sample_thumbnail_request(sample_image: bytes) -> dict[str, object]:
    """Return a valid multipart payload for the upload endpoint."""
    return {
        "files": [("files", ("sample.jpg", sample_image, "image/jpeg"))],
        "data": {"presets": "small,medium"},
    }
