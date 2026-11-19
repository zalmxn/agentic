from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class IntentType(str, Enum):
    SEARCH_SERVICE = "SEARCH_SERVICE"
    FIND_PROVIDER = "FIND_PROVIDER"
    FIND_CHEAPEST = "FIND_CHEAPEST"
    FIND_NEAREST = "FIND_NEAREST"
    CHECK_AVAILABILITY = "CHECK_AVAILABILITY"
    SELECT_PROVIDER = "SELECT_PROVIDER"
    BOOK_SERVICE = "BOOK_SERVICE"
    CONFIRM_BOOKING = "CONFIRM_BOOKING"
    CANCEL_BOOKING = "CANCEL_BOOKING"
    MODIFY_BOOKING = "MODIFY_BOOKING"
    CHECK_BOOKING_STATUS = "CHECK_BOOKING_STATUS"
    GENERAL_QUERY = "GENERAL_QUERY"
    UNKNOWN_INTENT = "UNKNOWN_INTENT"


class ConversationState(str, Enum):
    INITIAL = "INITIAL"
    COLLECTING_INFORMATION = "COLLECTING_INFORMATION"
    READY_TO_SEARCH = "READY_TO_SEARCH"
    SEARCHING = "SEARCHING"
    OPTIONS_FOUND = "OPTIONS_FOUND"
    PROVIDER_SELECTED = "PROVIDER_SELECTED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    BOOKING_IN_PROGRESS = "BOOKING_IN_PROGRESS"
    BOOKING_CONFIRMED = "BOOKING_CONFIRMED"
    BOOKING_FAILED = "BOOKING_FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ProviderReference(BaseModel):
    kind: Literal["index", "anaphoric", "qualifier", "name"]
    index: int | None = None  # 0-based index (e.g., second -> 1)
    qualifier: Literal["cheapest", "nearest", "highest_rated", "earliest"] | None = None
    provider_name: str | None = None
    raw: str = ""


class ExtractedEntities(BaseModel):
    service: str | None = None
    service_display: str | None = None
    service_category: str | None = None
    matched_services: list[str] = Field(default_factory=list)
    unknown_service_mention: str | None = None

    location: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    near_me: bool = False
    unresolved_location: str | None = None

    date: str | None = None  # YYYY-MM-DD
    time: str | None = None  # HH:MM
    date_ambiguity: str | None = None
    time_ambiguity: str | None = None
    invalid_date_reason: str | None = None

    price_preference: Literal["cheapest", "budget"] | None = None
    max_price: float | None = None
    distance_preference: Literal["nearest"] | None = None
    max_distance_km: float | None = None
    rating_preference: Literal["highest_rated"] | None = None
    availability_preference: Literal["earliest"] | None = None

    provider_name: str | None = None
    provider_reference: ProviderReference | None = None
    booking_id: str | None = None
    constraints: list[str] = Field(default_factory=list)


class ParsedIntent(BaseModel):
    type: IntentType
    confidence: float = Field(ge=0.0, le=1.0)
    normalized_text: str
    entities: ExtractedEntities = Field(default_factory=ExtractedEntities)
    ambiguities: list[str] = Field(default_factory=list)
    sort_by: Literal["price", "distance", "rating", "earliest", "best"] = "best"
