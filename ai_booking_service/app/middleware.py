import logging
import time
import uuid
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings

logger = logging.getLogger("ai_booking_service.middleware")


class SlidingWindowRateLimiter:
    """Lightweight in-memory per-client sliding-window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        bucket = self._hits[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


settings = get_settings()
rate_limiter = SlidingWindowRateLimiter(
    max_requests=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
)


async def request_context_and_rate_limit_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()

    # Exempt health/ready probes from rate limiting
    if request.url.path not in ("/health", "/ready"):
        client_host = request.client.host if request.client else "unknown"
        # Key by client IP + path prefix (never log raw auth token)
        limiter_key = f"{client_host}:{request.url.path.split('/')[1] if '/' in request.url.path else ''}"
        if not rate_limiter.allow(limiter_key):
            return JSONResponse(
                status_code=429,
                headers={"X-Request-ID": request_id},
                content={
                    "success": False,
                    "conversation_id": None,
                    "status": "ERROR",
                    "message": "Too many requests. Please wait a moment and try again.",
                    "error_code": "RATE_LIMITED",
                    "requires_confirmation": False,
                    "missing_fields": [],
                },
            )

    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "http_request",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": elapsed_ms,
        },
    )
    return response
