from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------- Auth ----------
class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    full_name: str = Field(..., min_length=2, max_length=150)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    is_active: bool
    created_at: datetime


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class VerifiedUserResponse(BaseModel):
    user_id: str
    email: str
    full_name: str


# ---------- Catalog ----------
class ServiceResponse(BaseModel):
    id: str
    code: str
    name: str
    category: str
    description: str
    default_duration_minutes: int


class ProviderSummary(BaseModel):
    provider_id: str
    listing_id: str | None = None
    provider_name: str
    location_name: str
    address: str
    phone_public: str | None = None
    latitude: float
    longitude: float
    rating: float | None = None
    review_count: int = 0
    work_start_time: str
    work_end_time: str
    verification_status: str | None = None
    source_name: str | None = None


# ---------- Internal Search & Availability ----------
class InternalSearchRequest(BaseModel):
    service_id: str | None = None
    service_code: str | None = None
    location_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    max_distance_km: float | None = Field(default=None, gt=0)
    max_price: float | None = Field(default=None, gt=0)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    sort_by: str = Field(default="best")
    limit: int = Field(default=10, ge=1, le=50)
    include_unavailable: bool = False


class ProviderSearchResultItem(BaseModel):
    provider_id: str
    listing_id: str | None = None
    provider_name: str
    location_name: str
    address: str
    phone_public: str | None = None
    latitude: float
    longitude: float
    distance_km: float | None = None
    rating: float | None = None
    review_count: int = 0
    verification_status: str | None = None
    source_name: str | None = None
    service_id: str
    service_code: str
    service_name: str
    provider_service_id: str
    price: float
    currency: str = "INR"
    duration_minutes: int
    slot_id: str | None = None
    slot_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    remaining_capacity: int = 0
    available: bool = False


class InternalSearchResponse(BaseModel):
    service_code: str | None = None
    service_name: str | None = None
    total_found: int
    providers: list[ProviderSearchResultItem]


class AvailabilityCheckRequest(BaseModel):
    provider_service_id: str
    slot_id: str | None = None
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class AvailabilityCheckResponse(BaseModel):
    available: bool
    reason: str | None = None
    provider_id: str | None = None
    provider_name: str | None = None
    provider_service_id: str
    slot_id: str | None = None
    slot_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    current_price: float | None = None
    currency: str = "INR"
    duration_minutes: int | None = None
    remaining_capacity: int = 0


# ---------- Bookings ----------
class InternalBookingCreateRequest(BaseModel):
    provider_service_id: str
    slot_id: str
    expected_price: float = Field(..., ge=0)
    idempotency_key: str = Field(..., min_length=8, max_length=128)
    notes: str | None = Field(default=None, max_length=500)


class BookingResponse(BaseModel):
    booking_id: str
    booking_code: str
    user_id: str
    provider_id: str
    provider_name: str
    service_code: str
    service_name: str
    provider_service_id: str
    slot_id: str
    service_date: str
    start_time: str
    end_time: str
    booked_price: float
    currency: str
    status: str
    idempotency_key: str
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None = None


class BookingCancelRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


# ---------- Internal Conversations ----------
class ConversationCreateRequest(BaseModel):
    state: str = "INITIAL"
    context: dict[str, Any] = Field(default_factory=dict)
    ttl_minutes: int = Field(default=30, ge=1, le=1440)


class ConversationUpdateRequest(BaseModel):
    state: str
    context: dict[str, Any]
    ttl_minutes: int | None = Field(default=None, ge=1, le=1440)


class ConversationRecordResponse(BaseModel):
    conversation_id: str
    user_id: str
    state: str
    context: dict[str, Any]
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


# ---------- Customer Chat Proxy ----------
class ChatMessageProxyRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    conversation_id: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class ChatConfirmProxyRequest(BaseModel):
    conversation_id: str
    confirm: bool = True


class ChatCancelProxyRequest(BaseModel):
    conversation_id: str | None = None
    booking_id: str | None = None
    reason: str | None = Field(default=None, max_length=300)
