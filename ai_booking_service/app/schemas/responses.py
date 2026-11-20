from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.intents import ConversationState, IntentType


class IntentSummary(BaseModel):
    type: IntentType
    confidence: float = 0.0
    service: str | None = None
    service_category: str | None = None
    location: str | None = None
    date: str | None = None
    time: str | None = None
    sort_by: str | None = None
    booking_id: str | None = None
    ambiguities: list[str] = Field(default_factory=list)


class ProviderOption(BaseModel):
    provider_id: str
    listing_id: str | None = None
    provider_name: str
    location_name: str = ""
    address: str = ""
    phone_public: str | None = None
    distance_km: float | None = None
    rating: float | None = None
    review_count: int = 0
    verification_status: str | None = None
    source_name: str | None = None
    service_id: str = ""
    service_code: str = ""
    service_name: str = ""
    provider_service_id: str
    price: float
    currency: str = "INR"
    duration_minutes: int = 60
    slot_id: str | None = None
    slot_date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    remaining_capacity: int = 0
    available: bool = True
    ranking_score: float | None = None
    ranking_explanation: str | None = None


class AssistantResponse(BaseModel):
    success: bool
    conversation_id: str | None = None
    status: str
    message: str
    intent: IntentSummary | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    error_code: str | None = None


class ConversationDetailResponse(BaseModel):
    success: bool = True
    conversation_id: str
    user_id: str
    status: ConversationState
    context: dict[str, Any]
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
