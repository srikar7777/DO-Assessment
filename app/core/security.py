"""API key authentication and per-client rate limiting."""

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

API_KEY_HEADER = "X-API-Key"

_request_log: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def require_api_key(request: Request) -> None:
    """Reject requests without a valid API key when one is configured."""
    settings = get_settings()
    expected = settings.api_key.strip()
    if not expected:
        return

    provided = request.headers.get(API_KEY_HEADER, "")
    if provided != expected:
        log.warning("api_key_rejected", path=request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def enforce_rate_limit(request: Request) -> None:
    """Apply a fixed-window per-client request limit when enabled."""
    settings = get_settings()
    limit = settings.rate_limit_requests_per_minute
    if limit <= 0:
        return

    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window_start = now - 60.0

    with _lock:
        timestamps = _request_log[client]
        while timestamps and timestamps[0] < window_start:
            timestamps.popleft()
        if len(timestamps) >= limit:
            log.warning("rate_limit_exceeded", client=client, limit=limit)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded, retry shortly",
            )
        timestamps.append(now)


def reset_rate_limiter() -> None:
    """Clear recorded request timestamps (used by tests)."""
    with _lock:
        _request_log.clear()
