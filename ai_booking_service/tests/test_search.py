from app.core.ranking_engine import RankingEngine
from app.schemas.responses import ProviderOption


def _sample_providers() -> list[ProviderOption]:
    return [
        ProviderOption(
            provider_id="p1",
            provider_name="ABC Car Wash",
            location_name="Aluva",
            distance_km=2.0,
            rating=4.5,
            review_count=100,
            provider_service_id="ps1",
            price=250.0,
            slot_id="s1",
            slot_date="2026-11-20",
            start_time="14:00",
            end_time="15:00",
            available=True,
        ),
        ProviderOption(
            provider_id="p2",
            provider_name="Sparkle Auto Spa",
            location_name="Aluva",
            distance_km=0.8,
            rating=4.9,
            review_count=220,
            provider_service_id="ps2",
            price=400.0,
            slot_id="s2",
            slot_date="2026-11-20",
            start_time="11:00",
            end_time="12:00",
            available=True,
        ),
        ProviderOption(
            provider_id="p3",
            provider_name="Unrated Budget Wash",
            location_name="Kochi",
            distance_km=12.0,
            rating=None,
            review_count=0,
            provider_service_id="ps3",
            price=220.0,
            slot_id="s3",
            slot_date="2026-11-20",
            start_time="16:00",
            end_time="17:00",
            available=True,
        ),
        ProviderOption(
            provider_id="p4",
            provider_name="Fully Booked Wash",
            location_name="Aluva",
            distance_km=0.2,
            rating=5.0,
            review_count=500,
            provider_service_id="ps4",
            price=100.0,
            slot_id=None,
            available=False,
        ),
    ]


def test_rank_cheapest_filters_unavailable():
    engine = RankingEngine()
    ranked = engine.rank(_sample_providers(), sort_by="price")
    assert len(ranked) == 3
    assert [p.provider_id for p in ranked] == ["p3", "p1", "p2"]
    assert all(p.available for p in ranked)
    assert "lowest price" in (ranked[0].ranking_explanation or "")


def test_rank_nearest_with_max_distance():
    engine = RankingEngine()
    ranked = engine.rank(_sample_providers(), sort_by="distance", max_distance_km=5.0)
    assert [p.provider_id for p in ranked] == ["p2", "p1"]
    assert "nearest distance" in (ranked[0].ranking_explanation or "")


def test_rank_highest_rated_never_fabricates_rating():
    engine = RankingEngine()
    ranked = engine.rank(_sample_providers(), sort_by="rating")
    assert [p.provider_id for p in ranked] == ["p2", "p1", "p3"]
    assert ranked[2].rating is None
    assert "no rating yet" in (ranked[2].ranking_explanation or "")


def test_rank_earliest_slot():
    engine = RankingEngine()
    ranked = engine.rank(_sample_providers(), sort_by="earliest")
    assert ranked[0].provider_id == "p2"
    assert ranked[0].start_time == "11:00"


def test_rank_best_overall_explains_factors():
    engine = RankingEngine()
    ranked = engine.rank(_sample_providers(), sort_by="best")
    assert len(ranked) == 3
    for item in ranked:
        assert item.ranking_score is not None
        assert item.ranking_explanation is not None
        assert "Best overall score" in item.ranking_explanation
    # Ensure unrated provider does not mention fabricated rating
    unrated = next(p for p in ranked if p.provider_id == "p3")
    assert "rating=" not in (unrated.ranking_explanation or "")
