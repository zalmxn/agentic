from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.core.intent_parser import IntentParser
from app.repositories.conversation_repository import ConversationRecord, ConversationRepository
from app.schemas.intents import ConversationState
from app.services.backend_client import (
    BackendTimeoutError,
    BackendUnauthorizedError,
    PriceChangedError,
    SlotUnavailableError,
)
from app.services.booking_coordinator import BookingCoordinator
from app.services.conversation_manager import ConversationManager
from app.services.search_coordinator import SearchCoordinator


class MockRepo(ConversationRepository):
    def __init__(self):
        self.store: dict[str, ConversationRecord] = {}

    async def create(self, authorization: str, initial_context=None, request_id=None) -> ConversationRecord:
        now = datetime.now(timezone.utc)
        rec = ConversationRecord(
            conversation_id=str(uuid4()),
            user_id="user-1",
            state=ConversationState.INITIAL,
            context=dict(initial_context or {}),
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        self.store[rec.conversation_id] = rec
        return rec

    async def get(self, conversation_id: str, authorization: str, request_id=None) -> ConversationRecord:
        return self.store[conversation_id]

    async def save(self, record: ConversationRecord, authorization: str, request_id=None, refresh_ttl=True):
        self.store[record.conversation_id] = record
        return record


class ConfigurableBackend:
    def __init__(self):
        self.mode = "ok"
        self.booking_calls = 0
        self.seen_idempotency_keys: list[str] = []

    async def create_booking(self, payload: dict[str, Any], authorization: str, request_id=None):
        self.booking_calls += 1
        self.seen_idempotency_keys.append(payload["idempotency_key"])
        if self.mode == "slot_unavailable":
            raise SlotUnavailableError("Slot was just booked by another customer.")
        if self.mode == "price_changed":
            raise PriceChangedError(
                message="Price changed from 250 to 300 INR",
                expected_price=250.0,
                current_price=300.0,
                currency="INR",
            )
        if self.mode == "timeout":
            raise BackendTimeoutError()
        if self.mode == "unauthorized":
            raise BackendUnauthorizedError()

        return {
            "booking_id": "bk-100",
            "booking_code": "BK-20261120-9999",
            "user_id": "user-1",
            "provider_id": "p1",
            "provider_name": "ABC Car Wash",
            "service_code": "car_wash",
            "service_name": "Car Wash",
            "provider_service_id": payload["provider_service_id"],
            "slot_id": payload["slot_id"],
            "service_date": "2026-11-20",
            "start_time": "14:00",
            "end_time": "15:00",
            "booked_price": payload["expected_price"],
            "currency": "INR",
            "status": "CONFIRMED",
            "idempotency_key": payload["idempotency_key"],
        }

    async def cancel_booking(self, booking_id: str, reason, authorization: str, request_id=None):
        return {
            "booking_id": booking_id,
            "booking_code": "BK-20261120-9999",
            "provider_name": "ABC Car Wash",
            "status": "CANCELLED",
        }


async def _seed_awaiting_confirmation(repo: MockRepo) -> str:
    rec = await repo.create("Bearer tok")
    rec.state = ConversationState.AWAITING_CONFIRMATION
    rec.context = {
        "service": "car_wash",
        "service_display": "Car Wash",
        "selected_provider": {
            "provider_id": "p1",
            "provider_name": "ABC Car Wash",
            "service_name": "Car Wash",
            "provider_service_id": "ps-1",
            "slot_id": "slot-1",
            "slot_date": "2026-11-20",
            "start_time": "14:00",
            "end_time": "15:00",
            "expected_price": 250.0,
            "currency": "INR",
        },
    }
    await repo.save(rec, "Bearer tok")
    return rec.conversation_id


def _build_manager(backend: ConfigurableBackend):
    repo = MockRepo()
    manager = ConversationManager(
        repository=repo,
        search_coordinator=SearchCoordinator(backend),
        booking_coordinator=BookingCoordinator(backend),
        intent_parser=IntentParser(timezone_name="Asia/Kolkata"),
    )
    return manager, repo


@pytest.mark.asyncio
async def test_successful_booking_and_duplicate_confirmation_idempotency():
    backend = ConfigurableBackend()
    manager, repo = _build_manager(backend)
    conv_id = await _seed_awaiting_confirmation(repo)

    res1 = await manager.confirm_booking(conversation_id=conv_id, confirm=True, authorization="Bearer tok")
    assert res1.success is True
    assert res1.status == ConversationState.BOOKING_CONFIRMED.value
    assert backend.booking_calls == 1

    # Duplicate confirmation must replay safely without calling create_booking again
    res2 = await manager.confirm_booking(conversation_id=conv_id, confirm=True, authorization="Bearer tok")
    assert res2.success is True
    assert res2.status == ConversationState.BOOKING_CONFIRMED.value
    assert res2.data.get("idempotent_replay") is True
    assert backend.booking_calls == 1


@pytest.mark.asyncio
async def test_slot_unavailable_transitions_to_booking_failed():
    backend = ConfigurableBackend()
    backend.mode = "slot_unavailable"
    manager, repo = _build_manager(backend)
    conv_id = await _seed_awaiting_confirmation(repo)

    res = await manager.confirm_booking(conversation_id=conv_id, confirm=True, authorization="Bearer tok")
    assert res.success is False
    assert res.status == ConversationState.BOOKING_FAILED.value
    assert res.error_code == "SLOT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_price_changed_reprompts_and_succeeds_after_reconfirmation():
    backend = ConfigurableBackend()
    backend.mode = "price_changed"
    manager, repo = _build_manager(backend)
    conv_id = await _seed_awaiting_confirmation(repo)

    # First attempt hits price change -> stays AWAITING_CONFIRMATION with updated price
    res1 = await manager.confirm_booking(conversation_id=conv_id, confirm=True, authorization="Bearer tok")
    assert res1.success is False
    assert res1.status == ConversationState.AWAITING_CONFIRMATION.value
    assert res1.requires_confirmation is True
    assert res1.error_code == "PRICE_CHANGED"
    assert res1.data["current_price"] == 300.0
    assert repo.store[conv_id].context["selected_provider"]["expected_price"] == 300.0

    # Customer explicitly confirms updated price
    backend.mode = "ok"
    res2 = await manager.confirm_booking(conversation_id=conv_id, confirm=True, authorization="Bearer tok")
    assert res2.success is True
    assert res2.status == ConversationState.BOOKING_CONFIRMED.value
    assert res2.data["booking"]["booked_price"] == 300.0


@pytest.mark.asyncio
async def test_backend_timeout_handled_safely():
    backend = ConfigurableBackend()
    backend.mode = "timeout"
    manager, repo = _build_manager(backend)
    conv_id = await _seed_awaiting_confirmation(repo)

    res = await manager.confirm_booking(conversation_id=conv_id, confirm=True, authorization="Bearer tok")
    assert res.success is False
    assert res.status == ConversationState.BOOKING_FAILED.value
    assert res.error_code == "BACKEND_TIMEOUT"


@pytest.mark.asyncio
async def test_unauthorized_booking_error_handled():
    backend = ConfigurableBackend()
    backend.mode = "unauthorized"
    manager, repo = _build_manager(backend)
    conv_id = await _seed_awaiting_confirmation(repo)

    res = await manager.confirm_booking(conversation_id=conv_id, confirm=True, authorization="Bearer bad")
    assert res.success is False
    assert res.error_code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_cancel_confirmed_booking():
    backend = ConfigurableBackend()
    manager, repo = _build_manager(backend)
    conv_id = await _seed_awaiting_confirmation(repo)

    await manager.confirm_booking(conversation_id=conv_id, confirm=True, authorization="Bearer tok")
    cancelled = await manager.cancel_booking(
        conversation_id=conv_id,
        authorization="Bearer tok",
        reason="Plans changed",
    )
    assert cancelled.success is True
    assert cancelled.status == ConversationState.CANCELLED.value
    assert cancelled.data["booking"]["status"] == "CANCELLED"
