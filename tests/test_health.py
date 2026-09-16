"""Tests for the liveness and readiness endpoints."""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


def test_health_returns_service_metadata(client: TestClient) -> None:
    """Liveness returns the configured service identity."""
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "myservice-test"
    assert body["version"] == "0.0.0-test"
    assert body["environment"] == "development"


def test_health_includes_utc_timestamp(client: TestClient) -> None:
    """Liveness includes an ISO-8601 UTC timestamp."""
    body = client.get("/health").json()

    assert "timestamp" in body
    assert "T" in body["timestamp"]
    assert body["timestamp"].endswith("+00:00")


def test_ready_reports_database_ok(client: TestClient) -> None:
    """Readiness succeeds when the database answers SELECT 1."""
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "ok"


def test_ready_returns_503_when_database_unreachable(client: TestClient) -> None:
    """Readiness returns 503 when the database query raises."""
    failure = OperationalError("SELECT 1", {}, Exception("connection refused"))

    with patch("sqlalchemy.orm.Session.execute", side_effect=failure):
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["database"] == "unavailable"


def test_root_returns_service_links(client: TestClient) -> None:
    """The root endpoint advertises docs and health paths."""
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "myservice-test"
    assert body["docs"] == "/docs"
    assert body["health"] == "/health"
