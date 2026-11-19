from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base, create_sqlite_engine, get_db
from app.main import app
from app.models import AvailabilitySlot, Booking, ProviderService, User
from app.schemas import InternalBookingCreateRequest
from app.seed import seed_database
from app.security import create_access_token
from app.services.booking_service import BookingDomainError, cancel_booking_atomic, create_booking_atomic


@pytest.fixture
def test_db_factory(tmp_path: Path):
    db_file = tmp_path / "test_booking.db"
    engine = create_sqlite_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=engine)
    SessionTesting = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    db = SessionTesting()
    try:
        seed_database(db, days_ahead=3)
    finally:
        db.close()

    yield SessionTesting
    engine.dispose()


@pytest.fixture
def api_client(test_db_factory):
    def override_get_db():
        db = test_db_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # Bypass default lifespan seeding so tests never touch production/default booking_platform.db
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.clear()


def test_concurrent_booking_attempts_prevent_double_booking(test_db_factory):
    """
    8 concurrent threads attempt to book the exact same single-use availability slot.
    SQLite BEGIN IMMEDIATE + capacity constraints must guarantee exactly 1 succeeds.
    """
    db = test_db_factory()
    try:
        alice = db.query(User).filter(User.email == "alice@example.com").first()
        ps = db.query(ProviderService).first()
        slot = (
            db.query(AvailabilitySlot)
            .filter(
                AvailabilitySlot.provider_id == ps.provider_id,
                AvailabilitySlot.booked_count == 0,
            )
            .first()
        )
        assert alice is not None and ps is not None and slot is not None
        user_id = alice.id
        ps_id = ps.id
        slot_id = slot.id
        price = ps.price
    finally:
        db.close()

    def attempt_booking(idx: int) -> tuple[str, str]:
        local_db = test_db_factory()
        try:
            req = InternalBookingCreateRequest(
                provider_service_id=ps_id,
                slot_id=slot_id,
                expected_price=price,
                idempotency_key=f"concurrent-key-{idx:04d}",
            )
            res = create_booking_atomic(local_db, user_id, req)
            return ("OK", res.booking_id)
        except BookingDomainError as exc:
            return ("ERR", exc.code)
        finally:
            local_db.close()

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(attempt_booking, i) for i in range(8)]
        for f in as_completed(futures):
            results.append(f.result())

    successes = [r for r in results if r[0] == "OK"]
    failures = [r for r in results if r[0] == "ERR"]

    assert len(successes) == 1
    assert len(failures) == 7
    assert all(code in ("SLOT_UNAVAILABLE", "DUPLICATE_BOOKING", "BOOKING_CONFLICT") for _, code in failures)

    # Verify DB invariants
    verify_db = test_db_factory()
    try:
        refreshed_slot = verify_db.query(AvailabilitySlot).filter(AvailabilitySlot.id == slot_id).first()
        booking_count = verify_db.query(Booking).filter(Booking.slot_id == slot_id).count()
        assert refreshed_slot.booked_count == 1
        assert booking_count == 1
    finally:
        verify_db.close()


def test_idempotent_retry_and_mismatch_protection(test_db_factory):
    db = test_db_factory()
    try:
        alice = db.query(User).filter(User.email == "alice@example.com").first()
        ps = db.query(ProviderService).first()
        slot = db.query(AvailabilitySlot).filter(AvailabilitySlot.provider_id == ps.provider_id).first()

        req = InternalBookingCreateRequest(
            provider_service_id=ps.id,
            slot_id=slot.id,
            expected_price=ps.price,
            idempotency_key="idem-retry-12345678",
        )
        b1 = create_booking_atomic(db, alice.id, req)
        b2 = create_booking_atomic(db, alice.id, req)
        assert b1.booking_id == b2.booking_id

        # Same idempotency key with different expected_price must be rejected
        bad_req = InternalBookingCreateRequest(
            provider_service_id=ps.id,
            slot_id=slot.id,
            expected_price=ps.price + 10.0,
            idempotency_key="idem-retry-12345678",
        )
        with pytest.raises(BookingDomainError) as exc_info:
            create_booking_atomic(db, alice.id, bad_req)
        assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"
    finally:
        db.close()


def test_price_change_triggers_rollback(test_db_factory):
    db = test_db_factory()
    try:
        alice = db.query(User).filter(User.email == "alice@example.com").first()
        ps = db.query(ProviderService).first()
        slot = db.query(AvailabilitySlot).filter(AvailabilitySlot.provider_id == ps.provider_id).first()

        req = InternalBookingCreateRequest(
            provider_service_id=ps.id,
            slot_id=slot.id,
            expected_price=ps.price - 50.0,  # stale price
            idempotency_key="idem-price-change-01",
        )
        with pytest.raises(BookingDomainError) as exc_info:
            create_booking_atomic(db, alice.id, req)

        assert exc_info.value.code == "PRICE_CHANGED"
        assert exc_info.value.details["current_price"] == ps.price

        # Verify rollback left slot untouched
        refreshed = db.query(AvailabilitySlot).filter(AvailabilitySlot.id == slot.id).first()
        assert refreshed.booked_count == 0
        assert db.query(Booking).filter(Booking.slot_id == slot.id).count() == 0
    finally:
        db.close()


def test_cancellation_releases_slot_and_enforces_ownership(test_db_factory):
    db = test_db_factory()
    try:
        alice = db.query(User).filter(User.email == "alice@example.com").first()
        bob = db.query(User).filter(User.email == "bob@example.com").first()
        ps = db.query(ProviderService).first()
        slot = db.query(AvailabilitySlot).filter(AvailabilitySlot.provider_id == ps.provider_id).first()

        booking = create_booking_atomic(
            db,
            alice.id,
            InternalBookingCreateRequest(
                provider_service_id=ps.id,
                slot_id=slot.id,
                expected_price=ps.price,
                idempotency_key="idem-owner-check-01",
            ),
        )

        # Bob cannot cancel Alice's booking
        with pytest.raises(BookingDomainError) as exc_info:
            cancel_booking_atomic(db, booking.booking_id, bob.id, reason="Unauthorized")
        assert exc_info.value.code == "FORBIDDEN"

        # Alice cancels her booking -> slot re-opens
        cancelled = cancel_booking_atomic(db, booking.booking_id, alice.id, reason="Change of plans")
        assert cancelled.status == "CANCELLED"
        refreshed = db.query(AvailabilitySlot).filter(AvailabilitySlot.id == slot.id).first()
        assert refreshed.booked_count == 0

        # Bob can now legitimately book the released slot
        bob_booking = create_booking_atomic(
            db,
            bob.id,
            InternalBookingCreateRequest(
                provider_service_id=ps.id,
                slot_id=slot.id,
                expected_price=ps.price,
                idempotency_key="idem-bob-rebook-01",
            ),
        )
        assert bob_booking.status == "CONFIRMED"
        assert bob_booking.user_id == bob.id
    finally:
        db.close()


def test_internal_endpoints_require_token_and_enforce_conversation_isolation(api_client: TestClient, test_db_factory):
    settings = get_settings()
    internal_headers = {"X-Internal-Token": settings.internal_api_token}

    # Missing internal token -> 401
    unauth = api_client.post("/internal/services/search", json={"service_code": "car_wash"})
    assert unauth.status_code == 401

    # Wrong internal token -> 401
    wrong = api_client.post(
        "/internal/services/search",
        headers={"X-Internal-Token": "wrong-token"},
        json={"service_code": "car_wash"},
    )
    assert wrong.status_code == 401

    # Valid internal search -> 200
    ok = api_client.post(
        "/internal/services/search",
        headers=internal_headers,
        json={"service_code": "car_wash", "location_name": "Aluva", "sort_by": "price"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["total_found"] >= 2
    assert body["providers"][0]["price"] <= body["providers"][1]["price"]

    # Conversation cross-user isolation over HTTP
    db = test_db_factory()
    try:
        alice = db.query(User).filter(User.email == "alice@example.com").first()
        bob = db.query(User).filter(User.email == "bob@example.com").first()
        alice_tok = create_access_token(alice.id, alice.email)
        bob_tok = create_access_token(bob.id, bob.email)
    finally:
        db.close()

    c_resp = api_client.post(
        "/internal/conversations",
        headers={**internal_headers, "Authorization": f"Bearer {alice_tok}"},
        json={"state": "INITIAL", "context": {"service": "car_wash"}, "ttl_minutes": 30},
    )
    assert c_resp.status_code == 201
    conv_id = c_resp.json()["conversation_id"]

    # Bob attempting to read Alice's conversation -> 403
    bob_get = api_client.get(
        f"/internal/conversations/{conv_id}",
        headers={**internal_headers, "Authorization": f"Bearer {bob_tok}"},
    )
    assert bob_get.status_code == 403
