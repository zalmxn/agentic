import hashlib
from typing import Any

from app.services.backend_client import BackendClient


class BookingCoordinator:
    """Handles availability verification, idempotent booking creation, status, and cancellation."""

    def __init__(self, backend_client: BackendClient):
        self.backend_client = backend_client

    @staticmethod
    def build_idempotency_key(
        user_id: str,
        conversation_id: str,
        provider_service_id: str,
        slot_id: str,
        expected_price: float,
    ) -> str:
        raw = f"{user_id}:{conversation_id}:{provider_service_id}:{slot_id}:{expected_price:.2f}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"idem-{digest}"

    async def verify_slot_availability(
        self,
        *,
        provider_service_id: str,
        slot_id: str | None = None,
        date: str | None = None,
        start_time: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "provider_service_id": provider_service_id,
            "slot_id": slot_id,
            "date": date,
            "start_time": start_time,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        return await self.backend_client.check_availability(payload, request_id=request_id)

    async def create_booking(
        self,
        *,
        user_id: str,
        conversation_id: str,
        provider_service_id: str,
        slot_id: str,
        expected_price: float,
        authorization: str,
        request_id: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        idempotency_key = self.build_idempotency_key(
            user_id=user_id,
            conversation_id=conversation_id,
            provider_service_id=provider_service_id,
            slot_id=slot_id,
            expected_price=expected_price,
        )
        payload: dict[str, Any] = {
            "provider_service_id": provider_service_id,
            "slot_id": slot_id,
            "expected_price": expected_price,
            "idempotency_key": idempotency_key,
        }
        if notes:
            payload["notes"] = notes

        return await self.backend_client.create_booking(
            payload=payload,
            authorization=authorization,
            request_id=request_id,
        )

    async def get_booking_status(
        self,
        *,
        booking_id: str | None,
        authorization: str,
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if booking_id:
            booking = await self.backend_client.get_booking(
                booking_id=booking_id,
                authorization=authorization,
                request_id=request_id,
            )
            return [booking]
        return await self.backend_client.list_bookings(
            authorization=authorization,
            request_id=request_id,
            limit=5,
        )

    async def cancel_booking(
        self,
        *,
        booking_id: str,
        reason: str | None,
        authorization: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.backend_client.cancel_booking(
            booking_id=booking_id,
            reason=reason,
            authorization=authorization,
            request_id=request_id,
        )
