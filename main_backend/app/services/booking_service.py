import hashlib
import math
import secrets
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import immediate_transaction
from app.models import AvailabilitySlot, Booking, Provider, ProviderService, Service
from app.schemas import (
    AvailabilityCheckRequest,
    AvailabilityCheckResponse,
    BookingResponse,
    InternalBookingCreateRequest,
    InternalSearchRequest,
    InternalSearchResponse,
    ProviderSearchResultItem,
)


class BookingDomainError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return round(2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


def _time_to_minutes(hhmm: str) -> int:
    hours, minutes = map(int, hhmm.split(":"))
    return hours * 60 + minutes


def _build_booking_response(booking: Booking) -> BookingResponse:
    provider_name = booking.provider.name if booking.provider else ""
    service_code = (
        booking.provider_service.service.code
        if booking.provider_service and booking.provider_service.service
        else ""
    )
    service_name = (
        booking.provider_service.service.name
        if booking.provider_service and booking.provider_service.service
        else ""
    )
    return BookingResponse(
        booking_id=booking.id,
        booking_code=booking.booking_code,
        user_id=booking.user_id,
        provider_id=booking.provider_id,
        provider_name=provider_name,
        service_code=service_code,
        service_name=service_name,
        provider_service_id=booking.provider_service_id,
        slot_id=booking.slot_id,
        service_date=booking.service_date,
        start_time=booking.start_time,
        end_time=booking.end_time,
        booked_price=booking.booked_price,
        currency=booking.currency,
        status=booking.status,
        idempotency_key=booking.idempotency_key,
        notes=booking.notes,
        created_at=booking.created_at,
        updated_at=booking.updated_at,
        cancelled_at=booking.cancelled_at,
    )


def search_providers(db: Session, req: InternalSearchRequest) -> InternalSearchResponse:
    service: Service | None = None
    if req.service_id:
        service = db.query(Service).filter(Service.id == req.service_id).first()
    elif req.service_code:
        service = db.query(Service).filter(Service.code == req.service_code).first()

    query = (
        db.query(ProviderService)
        .join(Provider, ProviderService.provider_id == Provider.id)
        .join(Service, ProviderService.service_id == Service.id)
        .options(joinedload(ProviderService.provider), joinedload(ProviderService.service))
        .filter(ProviderService.is_active.is_(True), Provider.is_active.is_(True))
    )

    if service is not None:
        query = query.filter(ProviderService.service_id == service.id)
    elif req.service_id or req.service_code:
        return InternalSearchResponse(
            service_code=req.service_code,
            service_name=None,
            total_found=0,
            providers=[],
        )

    if req.max_price is not None:
        query = query.filter(ProviderService.price <= req.max_price)

    provider_services = query.all()

    results: list[ProviderSearchResultItem] = []
    for ps in provider_services:
        provider = ps.provider
        svc = ps.service

        distance_km: float | None = None
        if req.latitude is not None and req.longitude is not None:
            distance_km = haversine_km(req.latitude, req.longitude, provider.latitude, provider.longitude)
            if req.max_distance_km is not None and distance_km > req.max_distance_km:
                continue
        elif req.location_name:
            loc_lower = req.location_name.strip().lower()
            if (
                loc_lower not in provider.location_name.lower()
                and loc_lower not in provider.address.lower()
            ):
                continue

        # Find best matching slot for requested date/time
        slot_query = db.query(AvailabilitySlot).filter(
            AvailabilitySlot.provider_id == provider.id,
            AvailabilitySlot.is_active.is_(True),
        )
        if req.date:
            slot_query = slot_query.filter(AvailabilitySlot.slot_date == req.date)
        if req.start_time:
            slot_query = slot_query.filter(AvailabilitySlot.start_time == req.start_time)

        slots = slot_query.order_by(
            AvailabilitySlot.slot_date.asc(), AvailabilitySlot.start_time.asc()
        ).all()

        chosen_slot: AvailabilitySlot | None = None
        for slot in slots:
            if slot.booked_count < slot.capacity:
                chosen_slot = slot
                break

        available = chosen_slot is not None
        if not available and not req.include_unavailable:
            continue

        fallback_slot = chosen_slot or (slots[0] if slots else None)
        remaining = (
            max(0, fallback_slot.capacity - fallback_slot.booked_count)
            if fallback_slot is not None
            else 0
        )

        results.append(
            ProviderSearchResultItem(
                provider_id=provider.id,
                provider_name=provider.name,
                location_name=provider.location_name,
                address=provider.address,
                latitude=provider.latitude,
                longitude=provider.longitude,
                distance_km=distance_km,
                rating=provider.rating,
                review_count=provider.review_count,
                service_id=svc.id,
                service_code=svc.code,
                service_name=svc.name,
                provider_service_id=ps.id,
                price=ps.price,
                currency=ps.currency,
                duration_minutes=ps.duration_minutes,
                slot_id=fallback_slot.id if fallback_slot else None,
                slot_date=fallback_slot.slot_date if fallback_slot else None,
                start_time=fallback_slot.start_time if fallback_slot else None,
                end_time=fallback_slot.end_time if fallback_slot else None,
                remaining_capacity=remaining,
                available=available,
            )
        )

    # Sort at backend level for deterministic pre-ranking
    sort_by = (req.sort_by or "best").lower()
    if sort_by == "price":
        results.sort(key=lambda x: (not x.available, x.price, x.distance_km if x.distance_km is not None else 9999.0))
    elif sort_by == "distance":
        results.sort(key=lambda x: (not x.available, x.distance_km if x.distance_km is not None else 9999.0, x.price))
    elif sort_by == "rating":
        results.sort(key=lambda x: (not x.available, -(x.rating if x.rating is not None else -1.0), x.price))
    elif sort_by == "earliest":
        results.sort(
            key=lambda x: (
                not x.available,
                x.slot_date or "9999-12-31",
                x.start_time or "23:59",
                x.price,
            )
        )
    else:
        results.sort(key=lambda x: (not x.available, x.price, -(x.rating or 0.0)))

    total_found = len(results)
    limited = results[: req.limit]
    return InternalSearchResponse(
        service_code=service.code if service else req.service_code,
        service_name=service.name if service else None,
        total_found=total_found,
        providers=limited,
    )


def check_availability(db: Session, req: AvailabilityCheckRequest) -> AvailabilityCheckResponse:
    ps = (
        db.query(ProviderService)
        .options(joinedload(ProviderService.provider), joinedload(ProviderService.service))
        .filter(ProviderService.id == req.provider_service_id)
        .first()
    )
    if not ps or not ps.is_active or not ps.provider or not ps.provider.is_active:
        return AvailabilityCheckResponse(
            available=False,
            reason="Provider or service offering is inactive or not found.",
            provider_service_id=req.provider_service_id,
        )

    provider = ps.provider
    slot: AvailabilitySlot | None = None
    if req.slot_id:
        slot = (
            db.query(AvailabilitySlot)
            .filter(
                AvailabilitySlot.id == req.slot_id,
                AvailabilitySlot.provider_id == provider.id,
            )
            .first()
        )
    elif req.date and req.start_time:
        slot = (
            db.query(AvailabilitySlot)
            .filter(
                AvailabilitySlot.provider_id == provider.id,
                AvailabilitySlot.slot_date == req.date,
                AvailabilitySlot.start_time == req.start_time,
            )
            .first()
        )

    if not slot:
        return AvailabilityCheckResponse(
            available=False,
            reason="Requested time slot was not found for this provider.",
            provider_id=provider.id,
            provider_name=provider.name,
            provider_service_id=ps.id,
            current_price=ps.price,
            currency=ps.currency,
            duration_minutes=ps.duration_minutes,
        )

    if not slot.is_active:
        return AvailabilityCheckResponse(
            available=False,
            reason="Requested time slot is inactive.",
            provider_id=provider.id,
            provider_name=provider.name,
            provider_service_id=ps.id,
            slot_id=slot.id,
            slot_date=slot.slot_date,
            start_time=slot.start_time,
            end_time=slot.end_time,
            current_price=ps.price,
            currency=ps.currency,
            duration_minutes=ps.duration_minutes,
        )

    # Validate provider working hours & service duration
    start_min = _time_to_minutes(slot.start_time)
    work_start_min = _time_to_minutes(provider.work_start_time)
    work_end_min = _time_to_minutes(provider.work_end_time)
    if start_min < work_start_min or (start_min + ps.duration_minutes) > work_end_min:
        return AvailabilityCheckResponse(
            available=False,
            reason="Service duration exceeds provider working hours.",
            provider_id=provider.id,
            provider_name=provider.name,
            provider_service_id=ps.id,
            slot_id=slot.id,
            slot_date=slot.slot_date,
            start_time=slot.start_time,
            end_time=slot.end_time,
            current_price=ps.price,
            currency=ps.currency,
            duration_minutes=ps.duration_minutes,
        )

    remaining = max(0, slot.capacity - slot.booked_count)
    if remaining <= 0:
        return AvailabilityCheckResponse(
            available=False,
            reason="Time slot is already fully booked.",
            provider_id=provider.id,
            provider_name=provider.name,
            provider_service_id=ps.id,
            slot_id=slot.id,
            slot_date=slot.slot_date,
            start_time=slot.start_time,
            end_time=slot.end_time,
            current_price=ps.price,
            currency=ps.currency,
            duration_minutes=ps.duration_minutes,
            remaining_capacity=0,
        )

    return AvailabilityCheckResponse(
        available=True,
        provider_id=provider.id,
        provider_name=provider.name,
        provider_service_id=ps.id,
        slot_id=slot.id,
        slot_date=slot.slot_date,
        start_time=slot.start_time,
        end_time=slot.end_time,
        current_price=ps.price,
        currency=ps.currency,
        duration_minutes=ps.duration_minutes,
        remaining_capacity=remaining,
    )


def _request_fingerprint(user_id: str, req: InternalBookingCreateRequest) -> str:
    raw = f"{user_id}:{req.provider_service_id}:{req.slot_id}:{req.expected_price:.2f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_booking_code(slot_date: str) -> str:
    compact_date = slot_date.replace("-", "")
    suffix = secrets.token_hex(2).upper()
    return f"BK-{compact_date}-{suffix}"


def create_booking_atomic(
    db: Session,
    user_id: str,
    req: InternalBookingCreateRequest,
) -> BookingResponse:
    """
    Concurrency-safe atomic booking creation inside SQLite BEGIN IMMEDIATE.
    Enforces:
      - Idempotency key replay & mismatch protection
      - Provider/service active validation
      - Working hours & duration validation
      - Strict expected_price revalidation
      - Atomic capacity reservation (no double booking)
      - Full transaction rollback on any failure
    """
    fingerprint = _request_fingerprint(user_id, req)

    try:
        with immediate_transaction(db):
            # 1. Idempotency check
            existing = (
                db.query(Booking)
                .options(
                    joinedload(Booking.provider),
                    joinedload(Booking.provider_service).joinedload(ProviderService.service),
                )
                .filter(Booking.idempotency_key == req.idempotency_key)
                .first()
            )
            if existing:
                if existing.user_id != user_id or existing.request_fingerprint != fingerprint:
                    raise BookingDomainError(
                        code="IDEMPOTENCY_CONFLICT",
                        message="Idempotency key was already used for a different booking request.",
                        status_code=409,
                    )
                return _build_booking_response(existing)

            # 2. Fetch and validate provider_service
            ps = (
                db.query(ProviderService)
                .options(
                    joinedload(ProviderService.provider),
                    joinedload(ProviderService.service),
                )
                .filter(ProviderService.id == req.provider_service_id)
                .first()
            )
            if not ps or not ps.is_active or not ps.provider or not ps.provider.is_active:
                raise BookingDomainError(
                    code="SERVICE_UNAVAILABLE",
                    message="Selected provider service is not available.",
                    status_code=404,
                )

            # 3. Revalidate current price against customer-confirmed expected_price
            if abs(ps.price - req.expected_price) > 0.01:
                raise BookingDomainError(
                    code="PRICE_CHANGED",
                    message=(
                        f"The price for {ps.provider.name} changed from "
                        f"{req.expected_price:.2f} to {ps.price:.2f} {ps.currency}."
                    ),
                    status_code=409,
                    details={
                        "expected_price": req.expected_price,
                        "current_price": ps.price,
                        "currency": ps.currency,
                    },
                )

            # 4. Fetch and validate availability slot inside write lock
            slot = (
                db.query(AvailabilitySlot)
                .filter(
                    AvailabilitySlot.id == req.slot_id,
                    AvailabilitySlot.provider_id == ps.provider_id,
                )
                .first()
            )
            if not slot or not slot.is_active:
                raise BookingDomainError(
                    code="SLOT_NOT_FOUND",
                    message="Selected availability slot does not exist or is inactive.",
                    status_code=404,
                )

            start_min = _time_to_minutes(slot.start_time)
            if (
                start_min < _time_to_minutes(ps.provider.work_start_time)
                or (start_min + ps.duration_minutes) > _time_to_minutes(ps.provider.work_end_time)
            ):
                raise BookingDomainError(
                    code="OUTSIDE_WORKING_HOURS",
                    message="Selected time slot falls outside the provider's working hours.",
                    status_code=409,
                )

            if slot.booked_count >= slot.capacity:
                raise BookingDomainError(
                    code="SLOT_UNAVAILABLE",
                    message="This time slot is no longer available.",
                    status_code=409,
                )

            # Prevent duplicate active booking by the same user for the same slot
            user_duplicate = (
                db.query(Booking)
                .filter(
                    Booking.user_id == user_id,
                    Booking.slot_id == slot.id,
                    Booking.status == "CONFIRMED",
                )
                .first()
            )
            if user_duplicate:
                raise BookingDomainError(
                    code="DUPLICATE_BOOKING",
                    message="You already have a confirmed booking for this time slot.",
                    status_code=409,
                )

            # 5. Reserve slot and create booking atomically
            slot.booked_count += 1
            booking = Booking(
                booking_code=_generate_booking_code(slot.slot_date),
                user_id=user_id,
                provider_id=ps.provider_id,
                provider_service_id=ps.id,
                slot_id=slot.id,
                service_date=slot.slot_date,
                start_time=slot.start_time,
                end_time=slot.end_time,
                booked_price=ps.price,
                currency=ps.currency,
                status="CONFIRMED",
                idempotency_key=req.idempotency_key,
                request_fingerprint=fingerprint,
                notes=req.notes,
            )
            db.add(booking)
            db.flush()

            # Ensure relationships are loaded before leaving transaction
            booking.provider = ps.provider
            booking.provider_service = ps
            response = _build_booking_response(booking)

        return response
    except IntegrityError as exc:
        raise BookingDomainError(
            code="BOOKING_CONFLICT",
            message="Booking could not be completed due to a concurrent update.",
            status_code=409,
        ) from exc


def get_booking_for_user(db: Session, booking_identifier: str, user_id: str) -> BookingResponse:
    booking = (
        db.query(Booking)
        .options(
            joinedload(Booking.provider),
            joinedload(Booking.provider_service).joinedload(ProviderService.service),
        )
        .filter(
            (Booking.id == booking_identifier) | (Booking.booking_code == booking_identifier)
        )
        .first()
    )
    if not booking:
        raise BookingDomainError(
            code="BOOKING_NOT_FOUND",
            message="Booking not found.",
            status_code=404,
        )
    if booking.user_id != user_id:
        raise BookingDomainError(
            code="FORBIDDEN",
            message="You are not authorized to access this booking.",
            status_code=403,
        )
    return _build_booking_response(booking)


def list_bookings_for_user(db: Session, user_id: str, limit: int = 20) -> list[BookingResponse]:
    bookings = (
        db.query(Booking)
        .options(
            joinedload(Booking.provider),
            joinedload(Booking.provider_service).joinedload(ProviderService.service),
        )
        .filter(Booking.user_id == user_id)
        .order_by(Booking.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_build_booking_response(b) for b in bookings]


def cancel_booking_atomic(
    db: Session,
    booking_identifier: str,
    user_id: str,
    reason: str | None = None,
) -> BookingResponse:
    with immediate_transaction(db):
        booking = (
            db.query(Booking)
            .options(
                joinedload(Booking.provider),
                joinedload(Booking.provider_service).joinedload(ProviderService.service),
                joinedload(Booking.slot),
            )
            .filter(
                (Booking.id == booking_identifier) | (Booking.booking_code == booking_identifier)
            )
            .first()
        )
        if not booking:
            raise BookingDomainError(
                code="BOOKING_NOT_FOUND",
                message="Booking not found.",
                status_code=404,
            )
        if booking.user_id != user_id:
            raise BookingDomainError(
                code="FORBIDDEN",
                message="You are not authorized to cancel this booking.",
                status_code=403,
            )
        if booking.status == "CANCELLED":
            return _build_booking_response(booking)
        if booking.status != "CONFIRMED":
            raise BookingDomainError(
                code="INVALID_CANCELLATION",
                message=f"Booking in status '{booking.status}' cannot be cancelled.",
                status_code=409,
            )

        booking.status = "CANCELLED"
        booking.cancelled_at = datetime.now(timezone.utc)
        if reason:
            booking.notes = f"{booking.notes or ''} [Cancelled: {reason}]".strip()

        if booking.slot and booking.slot.booked_count > 0:
            booking.slot.booked_count -= 1

        db.flush()
        response = _build_booking_response(booking)

    return response
