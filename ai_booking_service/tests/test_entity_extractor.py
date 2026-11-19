from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.entity_extractor import EntityExtractor

TZ = ZoneInfo("Asia/Kolkata")
REF_NOW = datetime(2026, 11, 19, 10, 0, tzinfo=TZ)


@pytest.fixture
def extractor() -> EntityExtractor:
    return EntityExtractor(timezone_name="Asia/Kolkata")


def test_extract_full_booking_query(extractor: EntityExtractor):
    ent = extractor.extract(
        "Find the cheapest car wash near Aluva tomorrow at 2:30 PM under ₹300 within 5 km",
        reference_now=REF_NOW,
    )
    assert ent.service == "car_wash"
    assert ent.service_category == "automotive"
    assert ent.location == "Aluva"
    assert ent.latitude == pytest.approx(10.1076)
    assert ent.longitude == pytest.approx(76.3516)
    assert ent.date == "2026-11-20"
    assert ent.time == "14:30"
    assert ent.price_preference == "cheapest"
    assert ent.max_price == 300.0
    assert ent.max_distance_km == 5.0


def test_extract_24h_time_and_iso_date(extractor: EntityExtractor):
    ent = extractor.extract("AC service on 2026-11-25 at 16:00", reference_now=REF_NOW)
    assert ent.service == "ac_repair"
    assert ent.date == "2026-11-25"
    assert ent.time == "16:00"


def test_extract_provider_references(extractor: EntityExtractor):
    e1 = extractor.extract("Book the second provider", reference_now=REF_NOW)
    assert e1.provider_reference is not None
    assert e1.provider_reference.kind == "index"
    assert e1.provider_reference.index == 1

    e2 = extractor.extract("Select the cheapest one", reference_now=REF_NOW)
    assert e2.provider_reference is not None
    assert e2.provider_reference.kind == "qualifier"
    assert e2.provider_reference.qualifier == "cheapest"

    e3 = extractor.extract("Book this provider", reference_now=REF_NOW)
    assert e3.provider_reference is not None
    assert e3.provider_reference.kind == "anaphoric"

    e4 = extractor.extract(
        "Go with Sparkle Auto Spa",
        reference_now=REF_NOW,
        known_provider_names=["ABC Car Wash", "Sparkle Auto Spa"],
    )
    assert e4.provider_reference is not None
    assert e4.provider_reference.kind == "name"
    assert e4.provider_reference.provider_name == "Sparkle Auto Spa"


def test_extract_booking_identifier(extractor: EntityExtractor):
    e1 = extractor.extract("Cancel booking BK-20261120-A1B2 please", reference_now=REF_NOW)
    assert e1.booking_id == "BK-20261120-A1B2"

    uuid_val = "123e4567-e89b-12d3-a456-426614174000"
    e2 = extractor.extract(f"Status of {uuid_val}", reference_now=REF_NOW)
    assert e2.booking_id == uuid_val


def test_extract_vague_date_and_time_does_not_fabricate(extractor: EntityExtractor):
    ent = extractor.extract("I need plumbing next week evening", reference_now=REF_NOW)
    assert ent.service == "plumbing"
    assert ent.date is None
    assert ent.time is None
    assert ent.date_ambiguity is not None
    assert ent.time_ambiguity is not None
