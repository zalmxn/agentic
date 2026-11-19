import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Conversation
from app.schemas import (
    AvailabilityCheckRequest,
    AvailabilityCheckResponse,
    BookingCancelRequest,
    BookingResponse,
    ConversationCreateRequest,
    ConversationRecordResponse,
    ConversationUpdateRequest,
    InternalBookingCreateRequest,
    InternalSearchRequest,
    InternalSearchResponse,
    VerifiedUserResponse,
)
from app.security import resolve_user_from_bearer, verify_internal_token
from app.services.booking_service import (
    BookingDomainError,
    cancel_booking_atomic,
    check_availability,
    create_booking_atomic,
    get_booking_for_user,
    list_bookings_for_user,
    search_providers,
)

router = APIRouter(
    prefix="/internal",
    tags=["Internal Service APIs"],
    dependencies=[Depends(verify_internal_token)],
)


def _domain_error_response(exc: BookingDomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.code,
            "detail": exc.message,
            "details": exc.details,
        },
    )


@router.post("/auth/verify", response_model=VerifiedUserResponse)
def internal_verify_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> VerifiedUserResponse:
    user = resolve_user_from_bearer(authorization, db)
    return VerifiedUserResponse(user_id=user.id, email=user.email, full_name=user.full_name)


@router.post("/services/search", response_model=InternalSearchResponse)
def internal_search_services(
    payload: InternalSearchRequest,
    db: Session = Depends(get_db),
) -> InternalSearchResponse:
    return search_providers(db, payload)


@router.post("/availability/check", response_model=AvailabilityCheckResponse)
def internal_check_availability(
    payload: AvailabilityCheckRequest,
    db: Session = Depends(get_db),
) -> AvailabilityCheckResponse:
    return check_availability(db, payload)


@router.post("/bookings/create", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
def internal_create_booking(
    payload: InternalBookingCreateRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    user = resolve_user_from_bearer(authorization, db)
    try:
        return create_booking_atomic(db, user.id, payload)
    except BookingDomainError as exc:
        return _domain_error_response(exc)


@router.get("/bookings", response_model=list[BookingResponse])
def internal_list_bookings(
    limit: int = Query(default=20, ge=1, le=50),
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> list[BookingResponse]:
    user = resolve_user_from_bearer(authorization, db)
    return list_bookings_for_user(db, user.id, limit=limit)


@router.get("/bookings/{booking_id}", response_model=BookingResponse)
def internal_get_booking(
    booking_id: str,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    user = resolve_user_from_bearer(authorization, db)
    try:
        return get_booking_for_user(db, booking_id, user.id)
    except BookingDomainError as exc:
        return _domain_error_response(exc)


@router.post("/bookings/{booking_id}/cancel", response_model=BookingResponse)
def internal_cancel_booking(
    booking_id: str,
    payload: BookingCancelRequest | None = None,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    user = resolve_user_from_bearer(authorization, db)
    try:
        return cancel_booking_atomic(
            db,
            booking_id,
            user.id,
            reason=payload.reason if payload else None,
        )
    except BookingDomainError as exc:
        return _domain_error_response(exc)


# ---------- Persistent Conversation Store (Stateless AI Microservice) ----------
def _to_conversation_response(conv: Conversation) -> ConversationRecordResponse:
    try:
        context = json.loads(conv.context_json) if conv.context_json else {}
    except json.JSONDecodeError:
        context = {}
    return ConversationRecordResponse(
        conversation_id=conv.id,
        user_id=conv.user_id,
        state=conv.state,
        context=context,
        expires_at=conv.expires_at,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@router.post(
    "/conversations",
    response_model=ConversationRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def internal_create_conversation(
    payload: ConversationCreateRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> ConversationRecordResponse:
    user = resolve_user_from_bearer(authorization, db)
    now = datetime.now(timezone.utc)
    conv = Conversation(
        user_id=user.id,
        state=payload.state,
        context_json=json.dumps(payload.context),
        expires_at=now + timedelta(minutes=payload.ttl_minutes),
        created_at=now,
        updated_at=now,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return _to_conversation_response(conv)


@router.get("/conversations/{conversation_id}", response_model=ConversationRecordResponse)
def internal_get_conversation(
    conversation_id: str,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> ConversationRecordResponse:
    user = resolve_user_from_bearer(authorization, db)
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conv.user_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to access this conversation.",
        )
    return _to_conversation_response(conv)


@router.put("/conversations/{conversation_id}", response_model=ConversationRecordResponse)
def internal_update_conversation(
    conversation_id: str,
    payload: ConversationUpdateRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> ConversationRecordResponse:
    user = resolve_user_from_bearer(authorization, db)
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    if conv.user_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You are not authorized to modify this conversation.",
        )
    now = datetime.now(timezone.utc)
    conv.state = payload.state
    conv.context_json = json.dumps(payload.context)
    conv.updated_at = now
    if payload.ttl_minutes is not None:
        conv.expires_at = now + timedelta(minutes=payload.ttl_minutes)
    db.commit()
    db.refresh(conv)
    return _to_conversation_response(conv)
