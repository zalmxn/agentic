from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.core.intent_parser import IntentParser
from app.repositories.conversation_repository import ConversationRecord, ConversationRepository
from app.schemas.intents import ConversationState
from app.services.backend_client import BackendForbiddenError
from app.services.booking_coordinator import BookingCoordinator
from app.services.conversation_manager import (
    ConversationManager,
    InvalidStateTransitionError,
    validate_transition,
)
from app.services.search_coordinator import SearchCoordinator


class InMemoryConversationRepo(ConversationRepository):
    """Mocked persistent conversation store enforcing user ownership for unit tests."""

    def __init__(self):
        self.store: dict[str, ConversationRecord] = {}

    @staticmethod
    def _user_from_auth(authorization: str) -> str:
        return authorization.replace("Bearer ", "").strip()

    async def create(self, authorization: str, initial_context=None, request_id=None) -> ConversationRecord:
        now = datetime.now(timezone.utc)
        rec = ConversationRecord(
            conversation_id=str(uuid4()),
            user_id=self._user_from_auth(authorization),
            state=ConversationState.INITIAL,
            context=dict(initial_context or {}),
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        self.store[rec.conversation_id] = rec
        return rec

    async def get(self, conversation_id: str, authorization: str, request_id=None) -> ConversationRecord:
        rec = self.store[conversation_id]
        if rec.user_id != self._user_from_auth(authorization):
            raise BackendForbiddenError("Conversation belongs to another user.")
        return rec

    async def save(self, record: ConversationRecord, authorization: str, request_id=None, refresh_ttl=True):
        if record.user_id != self._user_from_auth(authorization):
            raise BackendForbiddenError("Conversation belongs to another user.")
        record.updated_at = datetime.now(timezone.utc)
        self.store[record.conversation_id] = record
        return record


class FakeBackendClient:
    def __init__(self):
        self.providers = [
            {
                "provider_id": "prov-1",
                "provider_name": "ABC Car Wash",
                "location_name": "Aluva",
                "address": "Pump Junction, Aluva",
                "latitude": 10.1076,
                "longitude": 76.3516,
                "distance_km": 0.5,
                "rating": 4.6,
                "review_count": 140,
                "service_id": "svc-1",
                "service_code": "car_wash",
                "service_name": "Car Wash",
                "provider_service_id": "ps-1",
                "price": 250.0,
                "currency": "INR",
                "duration_minutes": 60,
                "slot_id": "slot-1",
                "slot_date": "2026-11-20",
                "start_time": "14:00",
                "end_time": "15:00",
                "remaining_capacity": 1,
                "available": True,
            },
            {
                "provider_id": "prov-2",
                "provider_name": "Sparkle Auto Spa",
                "location_name": "Aluva",
                "address": "Bank Junction, Aluva",
                "latitude": 10.1105,
                "longitude": 76.3542,
                "distance_km": 0.9,
                "rating": 4.8,
                "review_count": 210,
                "service_id": "svc-1",
                "service_code": "car_wash",
                "service_name": "Car Wash",
                "provider_service_id": "ps-2",
                "price": 350.0,
                "currency": "INR",
                "duration_minutes": 60,
                "slot_id": "slot-2",
                "slot_date": "2026-11-20",
                "start_time": "14:00",
                "end_time": "15:00",
                "remaining_capacity": 1,
                "available": True,
            },
        ]

    async def search_services(self, payload: dict[str, Any], request_id=None) -> dict[str, Any]:
        time_val = payload.get("start_time") or "14:00"
        items = []
        for p in self.providers:
            copy_p = dict(p)
            copy_p["start_time"] = time_val
            items.append(copy_p)
        return {"service_code": payload.get("service_code"), "total_found": len(items), "providers": items}

    async def check_availability(self, payload: dict[str, Any], request_id=None) -> dict[str, Any]:
        for p in self.providers:
            if p["provider_service_id"] == payload["provider_service_id"]:
                return {
                    "available": True,
                    "provider_id": p["provider_id"],
                    "provider_name": p["provider_name"],
                    "provider_service_id": p["provider_service_id"],
                    "slot_id": payload.get("slot_id") or p["slot_id"],
                    "slot_date": payload.get("date") or p["slot_date"],
                    "start_time": payload.get("start_time") or p["start_time"],
                    "end_time": p["end_time"],
                    "current_price": p["price"],
                    "currency": "INR",
                }
        return {"available": False, "reason": "Not found"}

    async def create_booking(self, payload: dict[str, Any], authorization: str, request_id=None):
        return {
            "booking_id": "bk-uuid-1",
            "booking_code": "BK-20261120-ABCD",
            "user_id": authorization.replace("Bearer ", ""),
            "provider_id": "prov-1",
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


def _make_manager():
    repo = InMemoryConversationRepo()
    backend = FakeBackendClient()
    manager = ConversationManager(
        repository=repo,
        search_coordinator=SearchCoordinator(backend),
        booking_coordinator=BookingCoordinator(backend),
        intent_parser=IntentParser(timezone_name="Asia/Kolkata"),
    )
    return manager, repo


@pytest.mark.asyncio
async def test_multi_message_booking_conversation():
    manager, _ = _make_manager()
    auth = "Bearer user-alice"
    ref_now = datetime(2026, 11, 19, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    # Turn 1: Missing date & time
    r1 = await manager.handle_message(
        message="Find the cheapest car wash near Aluva.",
        authorization=auth,
        reference_now=ref_now,
    )
    assert r1.success is True
    assert r1.status == ConversationState.COLLECTING_INFORMATION.value
    assert set(r1.missing_fields) == {"date", "time"}
    conv_id = r1.conversation_id
    assert conv_id is not None

    # Turn 2: Provide date & time
    r2 = await manager.handle_message(
        message="Tomorrow at 2 PM.",
        authorization=auth,
        conversation_id=conv_id,
        reference_now=ref_now,
    )
    assert r2.success is True
    assert r2.status == ConversationState.OPTIONS_FOUND.value
    assert len(r2.data["providers"]) == 2
    assert r2.data["providers"][0]["provider_name"] == "ABC Car Wash"

    # Turn 3: Anaphoric reference "Book that one"
    r3 = await manager.handle_message(
        message="Book that one.",
        authorization=auth,
        conversation_id=conv_id,
        reference_now=ref_now,
    )
    assert r3.success is True
    assert r3.status == ConversationState.AWAITING_CONFIRMATION.value
    assert r3.requires_confirmation is True
    assert r3.data["selected_provider"]["provider_name"] == "ABC Car Wash"
    assert r3.data["selected_provider"]["expected_price"] == 250.0

    # Turn 4: Confirm via "Yes"
    r4 = await manager.handle_message(
        message="Yes",
        authorization=auth,
        conversation_id=conv_id,
        reference_now=ref_now,
    )
    assert r4.success is True
    assert r4.status == ConversationState.BOOKING_CONFIRMED.value
    assert r4.data["booking"]["booking_code"] == "BK-20261120-ABCD"


@pytest.mark.asyncio
async def test_ordinal_and_modify_time_reference_resolution():
    manager, _ = _make_manager()
    auth = "Bearer user-alice"
    ref_now = datetime(2026, 11, 19, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    r1 = await manager.handle_message(
        message="Find the cheapest car wash near Aluva tomorrow at 2 PM",
        authorization=auth,
        reference_now=ref_now,
    )
    assert r1.status == ConversationState.OPTIONS_FOUND.value

    # Select second provider
    r2 = await manager.handle_message(
        message="Book the second provider",
        authorization=auth,
        conversation_id=r1.conversation_id,
        reference_now=ref_now,
    )
    assert r2.status == ConversationState.AWAITING_CONFIRMATION.value
    assert r2.data["selected_provider"]["provider_name"] == "Sparkle Auto Spa"

    # Modify time to 4 PM
    r3 = await manager.handle_message(
        message="Change it to 4 PM",
        authorization=auth,
        conversation_id=r1.conversation_id,
        reference_now=ref_now,
    )
    assert r3.success is True
    assert r3.status == ConversationState.OPTIONS_FOUND.value
    assert r3.intent.time == "16:00"


@pytest.mark.asyncio
async def test_expired_conversation_rejected():
    manager, repo = _make_manager()
    auth = "Bearer user-alice"

    r1 = await manager.handle_message(message="Find a car wash near Aluva", authorization=auth)
    # Force expiration in repository
    repo.store[r1.conversation_id].expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    r2 = await manager.handle_message(
        message="Tomorrow at 2 PM",
        authorization=auth,
        conversation_id=r1.conversation_id,
    )
    assert r2.success is False
    assert r2.status == ConversationState.EXPIRED.value
    assert r2.error_code == "CONVERSATION_EXPIRED"


@pytest.mark.asyncio
async def test_cross_user_conversation_isolation():
    manager, _ = _make_manager()
    r1 = await manager.handle_message(
        message="Find a car wash near Aluva tomorrow at 2 PM",
        authorization="Bearer user-alice",
    )

    with pytest.raises(BackendForbiddenError):
        await manager.handle_message(
            message="Book that one",
            authorization="Bearer user-bob",
            conversation_id=r1.conversation_id,
        )


def test_invalid_state_transitions_rejected():
    with pytest.raises(InvalidStateTransitionError):
        validate_transition(ConversationState.INITIAL, ConversationState.BOOKING_CONFIRMED)

    with pytest.raises(InvalidStateTransitionError):
        validate_transition(ConversationState.COLLECTING_INFORMATION, ConversationState.BOOKING_IN_PROGRESS)

    with pytest.raises(InvalidStateTransitionError):
        validate_transition(ConversationState.EXPIRED, ConversationState.OPTIONS_FOUND)


@pytest.mark.asyncio
async def test_user_current_location_returned_and_used_for_near_me():
    manager, _ = _make_manager()
    auth = "Bearer user-alice"
    ref_now = datetime(2026, 11, 19, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    res = await manager.handle_message(
        message="Find the nearest car wash near me tomorrow at 2 PM",
        authorization=auth,
        latitude=10.0553,
        longitude=76.3219,
        location_label="Kalamassery",
        reference_now=ref_now,
    )
    assert res.success is True
    assert res.status == ConversationState.OPTIONS_FOUND.value
    assert res.data["user_location"] == {
        "label": "Kalamassery",
        "latitude": 10.0553,
        "longitude": 76.3219,
    }
    assert "from Kalamassery" in res.message
    assert res.data["providers"][0]["latitude"] == 10.1076
    assert res.data["providers"][0]["longitude"] == 76.3516


@pytest.mark.asyncio
async def test_list_all_providers_returns_results_without_requiring_datetime():
    manager, _ = _make_manager()
    auth = "Bearer user-alice"
    ref_now = datetime(2026, 11, 19, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    # "list all the car wash centres" should return OPTIONS_FOUND immediately with no date/time needed
    r1 = await manager.handle_message(
        message="list all the car wash centres",
        authorization=auth,
        reference_now=ref_now,
    )
    assert r1.success is True
    assert r1.status == ConversationState.OPTIONS_FOUND.value
    assert len(r1.data["providers"]) == 2
    assert "Here are all 2 available Car Wash providers" in r1.message

    # "list all the plumbers near me" with user GPS sorts by distance and returns user_location
    r2 = await manager.handle_message(
        message="list all the plumbers near me",
        authorization=auth,
        conversation_id=r1.conversation_id,
        latitude=10.0553,
        longitude=76.3219,
        location_label="Kalamassery",
        reference_now=ref_now,
    )
    assert r2.success is True
    assert r2.status == ConversationState.OPTIONS_FOUND.value
    assert r2.data["sort_by"] == "distance"
    assert r2.data["user_location"]["label"] == "Kalamassery"
    assert len(r2.data["providers"]) == 2
