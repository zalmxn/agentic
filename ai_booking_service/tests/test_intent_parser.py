from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.intent_parser import IntentParser
from app.schemas.intents import ConversationState, IntentType

TZ = ZoneInfo("Asia/Kolkata")
REF_NOW = datetime(2026, 11, 19, 10, 0, tzinfo=TZ)


@pytest.fixture
def parser() -> IntentParser:
    return IntentParser(timezone_name="Asia/Kolkata")


def test_find_car_wash(parser: IntentParser):
    res = parser.parse("Find a car wash.", reference_now=REF_NOW)
    assert res.type == IntentType.SEARCH_SERVICE
    assert res.entities.service == "car_wash"
    assert res.entities.location is None


def test_find_cheapest_car_wash(parser: IntentParser):
    res = parser.parse("Find the cheapest car wash.", reference_now=REF_NOW)
    assert res.type == IntentType.FIND_CHEAPEST
    assert res.entities.service == "car_wash"
    assert res.sort_by == "price"


def test_find_plumber_near_kochi(parser: IntentParser):
    res = parser.parse("Find a plumber near Kochi.", reference_now=REF_NOW)
    assert res.type == IntentType.SEARCH_SERVICE
    assert res.entities.service == "plumbing"
    assert res.entities.location == "Kochi"
    assert res.entities.latitude is not None


def test_need_ac_repair_tomorrow(parser: IntentParser):
    res = parser.parse("I need AC repair tomorrow.", reference_now=REF_NOW)
    assert res.type == IntentType.SEARCH_SERVICE
    assert res.entities.service == "ac_repair"
    assert res.entities.date == "2026-11-20"


def test_book_electrician_at_2pm(parser: IntentParser):
    res = parser.parse("Book an electrician at 2 PM.", reference_now=REF_NOW)
    assert res.type == IntentType.BOOK_SERVICE
    assert res.entities.service == "electrical"
    assert res.entities.time == "14:00"


def test_find_nearest_service_provider(parser: IntentParser):
    res = parser.parse("Find the nearest service provider.", reference_now=REF_NOW)
    assert res.type == IntentType.FIND_NEAREST
    assert res.sort_by == "distance"


def test_show_second_provider(parser: IntentParser):
    res = parser.parse("Show the second provider.", reference_now=REF_NOW)
    assert res.type == IntentType.SELECT_PROVIDER
    assert res.entities.provider_reference is not None
    assert res.entities.provider_reference.kind == "index"
    assert res.entities.provider_reference.index == 1


def test_book_that_one(parser: IntentParser):
    res = parser.parse("Book that one.", reference_now=REF_NOW)
    assert res.type == IntentType.SELECT_PROVIDER
    assert res.entities.provider_reference is not None
    assert res.entities.provider_reference.kind == "anaphoric"


def test_modify_booking_time(parser: IntentParser):
    res = parser.parse("Change the booking time to 4 PM.", reference_now=REF_NOW)
    assert res.type == IntentType.MODIFY_BOOKING
    assert res.entities.time == "16:00"


def test_check_booking_status(parser: IntentParser):
    res = parser.parse("Show my booking status.", reference_now=REF_NOW)
    assert res.type == IntentType.CHECK_BOOKING_STATUS


def test_find_available_near_me(parser: IntentParser):
    res = parser.parse("Find available car wash providers near me.", reference_now=REF_NOW)
    assert res.type == IntentType.CHECK_AVAILABILITY
    assert res.entities.service == "car_wash"
    assert res.entities.near_me is True


def test_lowest_priced_service(parser: IntentParser):
    res = parser.parse("Show me the lowest-priced service.", reference_now=REF_NOW)
    assert res.type == IntentType.FIND_CHEAPEST
    assert res.sort_by == "price"


def test_unknown_service_mention(parser: IntentParser):
    res = parser.parse("Find a dog walker near Aluva.", reference_now=REF_NOW)
    assert res.entities.service is None
    assert res.entities.unknown_service_mention == "dog walker"


def test_invalid_calendar_date(parser: IntentParser):
    res = parser.parse("Book a car wash on 2026-02-30 at 2 PM", reference_now=REF_NOW)
    assert res.entities.date is None
    assert res.entities.invalid_date_reason is not None
    assert len(res.ambiguities) >= 1


def test_past_date_rejected(parser: IntentParser):
    res = parser.parse("Find a plumber yesterday at 10 AM", reference_now=REF_NOW)
    assert res.entities.date is None
    assert res.entities.invalid_date_reason is not None


def test_ambiguous_time_detected_without_inventing(parser: IntentParser):
    res = parser.parse("I need car wash tomorrow morning", reference_now=REF_NOW)
    assert res.entities.date == "2026-11-20"
    assert res.entities.time is None
    assert res.entities.time_ambiguity is not None

    bare = parser.parse("Book electrician tomorrow at 4", reference_now=REF_NOW)
    assert bare.entities.time is None
    assert bare.entities.time_ambiguity is not None


def test_spelling_variations_handled(parser: IntentParser):
    res = parser.parse("Find the cheepest plumer near Alwaye tomorow at 2 pm!", reference_now=REF_NOW)
    assert res.type == IntentType.FIND_CHEAPEST
    assert res.entities.service == "plumbing"
    assert res.entities.location == "Aluva"
    assert res.entities.date == "2026-11-20"
    assert res.entities.time == "14:00"

    res2 = parser.parse("Need an electrcian for wiring", reference_now=REF_NOW)
    assert res2.entities.service == "electrical"


def test_multiple_service_mentions_flagged(parser: IntentParser):
    res = parser.parse("I need a plumber and an electrician tomorrow", reference_now=REF_NOW)
    assert set(res.entities.matched_services) == {"plumbing", "electrical"}
    assert any("Multiple services" in a for a in res.ambiguities)


def test_unsupported_request_returns_unknown_intent(parser: IntentParser):
    res = parser.parse("What is the capital of Mars?", reference_now=REF_NOW)
    assert res.type == IntentType.UNKNOWN_INTENT


def test_confirm_and_cancel_in_awaiting_confirmation(parser: IntentParser):
    yes = parser.parse("Yes", current_state=ConversationState.AWAITING_CONFIRMATION, reference_now=REF_NOW)
    assert yes.type == IntentType.CONFIRM_BOOKING

    no = parser.parse("No", current_state=ConversationState.AWAITING_CONFIRMATION, reference_now=REF_NOW)
    assert no.type == IntentType.CANCEL_BOOKING
