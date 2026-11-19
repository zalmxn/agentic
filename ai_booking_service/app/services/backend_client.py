import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("ai_booking_service.backend_client")


class BackendClientError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 502,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class BackendTimeoutError(BackendClientError):
    def __init__(self, message: str = "Main backend request timed out."):
        super().__init__("BACKEND_TIMEOUT", message, status_code=504)


class BackendUnavailableError(BackendClientError):
    def __init__(self, message: str = "Main backend service is unavailable."):
        super().__init__("BACKEND_UNAVAILABLE", message, status_code=503)


class BackendUnauthorizedError(BackendClientError):
    def __init__(self, message: str = "Authentication required or invalid credentials."):
        super().__init__("UNAUTHORIZED", message, status_code=401)


class BackendForbiddenError(BackendClientError):
    def __init__(self, message: str = "You do not have permission to access this resource."):
        super().__init__("FORBIDDEN", message, status_code=403)


class BackendNotFoundError(BackendClientError):
    def __init__(self, message: str = "Requested resource was not found."):
        super().__init__("NOT_FOUND", message, status_code=404)


class PriceChangedError(BackendClientError):
    def __init__(
        self,
        message: str,
        expected_price: float | None = None,
        current_price: float | None = None,
        currency: str = "INR",
    ):
        super().__init__(
            "PRICE_CHANGED",
            message,
            status_code=409,
            details={
                "expected_price": expected_price,
                "current_price": current_price,
                "currency": currency,
            },
        )
        self.expected_price = expected_price
        self.current_price = current_price
        self.currency = currency


class SlotUnavailableError(BackendClientError):
    def __init__(self, message: str = "Selected time slot is no longer available."):
        super().__init__("SLOT_UNAVAILABLE", message, status_code=409)


class BookingConflictError(BackendClientError):
    def __init__(self, code: str = "BOOKING_CONFLICT", message: str = "Booking conflict detected."):
        super().__init__(code, message, status_code=409)


class BackendClient:
    """
    Reusable async HTTPX client for authenticated communication with the main FastAPI backend.
    Never logs authorization headers or internal API tokens.
    """

    def __init__(
        self,
        base_url: str | None = None,
        internal_token: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.backend_url).rstrip("/")
        self.internal_token = internal_token or settings.internal_api_token
        self.timeout = timeout or settings.request_timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self, authorization: str | None = None, request_id: str | None = None) -> dict[str, str]:
        headers = {"X-Internal-Token": self.internal_token}
        if authorization:
            headers["Authorization"] = authorization
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authorization: str | None = None,
        request_id: str | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                headers=self._headers(authorization=authorization, request_id=request_id),
                json=json_body,
                params=params,
            )
        except httpx.TimeoutException as exc:
            logger.warning("backend_timeout", extra={"request_id": request_id, "path": path})
            raise BackendTimeoutError() from exc
        except httpx.HTTPError as exc:
            logger.error("backend_connection_error", extra={"request_id": request_id, "path": path})
            raise BackendUnavailableError() from exc

        if response.status_code < 400:
            return response.json()

        # Parse structured error safely without leaking internals
        try:
            err_body = response.json()
        except ValueError:
            err_body = {}

        err_code = err_body.get("error_code", "")
        detail = err_body.get("detail") or "Backend request failed."
        details = err_body.get("details") or {}

        if response.status_code == 401:
            raise BackendUnauthorizedError(detail)
        if response.status_code == 403:
            raise BackendForbiddenError(detail)
        if response.status_code == 404:
            raise BackendNotFoundError(detail)
        if response.status_code == 409:
            if err_code == "PRICE_CHANGED":
                raise PriceChangedError(
                    message=detail,
                    expected_price=details.get("expected_price"),
                    current_price=details.get("current_price"),
                    currency=details.get("currency", "INR"),
                )
            if err_code in ("SLOT_UNAVAILABLE", "OUTSIDE_WORKING_HOURS"):
                raise SlotUnavailableError(detail)
            raise BookingConflictError(code=err_code or "BOOKING_CONFLICT", message=detail)

        raise BackendClientError(
            code=err_code or "BACKEND_ERROR",
            message="The booking backend could not process this request.",
            status_code=502,
        )

    async def check_health(self) -> bool:
        try:
            resp = await self._client.get("/ready", timeout=3.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def verify_user(self, authorization: str, request_id: str | None = None) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/internal/auth/verify",
            authorization=authorization,
            request_id=request_id,
        )

    async def search_services(
        self,
        payload: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/internal/services/search",
            request_id=request_id,
            json_body=payload,
        )

    async def check_availability(
        self,
        payload: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/internal/availability/check",
            request_id=request_id,
            json_body=payload,
        )

    async def create_booking(
        self,
        payload: dict[str, Any],
        authorization: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/internal/bookings/create",
            authorization=authorization,
            request_id=request_id,
            json_body=payload,
        )

    async def get_booking(
        self,
        booking_id: str,
        authorization: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/internal/bookings/{booking_id}",
            authorization=authorization,
            request_id=request_id,
        )

    async def list_bookings(
        self,
        authorization: str,
        request_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            "/internal/bookings",
            authorization=authorization,
            request_id=request_id,
            params={"limit": limit},
        )

    async def cancel_booking(
        self,
        booking_id: str,
        reason: str | None,
        authorization: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body = {"reason": reason} if reason else {}
        return await self._request(
            "POST",
            f"/internal/bookings/{booking_id}/cancel",
            authorization=authorization,
            request_id=request_id,
            json_body=body,
        )

    async def create_conversation(
        self,
        state: str,
        context: dict[str, Any],
        ttl_minutes: int,
        authorization: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/internal/conversations",
            authorization=authorization,
            request_id=request_id,
            json_body={"state": state, "context": context, "ttl_minutes": ttl_minutes},
        )

    async def get_conversation(
        self,
        conversation_id: str,
        authorization: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/internal/conversations/{conversation_id}",
            authorization=authorization,
            request_id=request_id,
        )

    async def update_conversation(
        self,
        conversation_id: str,
        state: str,
        context: dict[str, Any],
        authorization: str,
        request_id: str | None = None,
        ttl_minutes: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"state": state, "context": context}
        if ttl_minutes is not None:
            body["ttl_minutes"] = ttl_minutes
        return await self._request(
            "PUT",
            f"/internal/conversations/{conversation_id}",
            authorization=authorization,
            request_id=request_id,
            json_body=body,
        )
