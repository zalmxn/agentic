from app.config import get_settings
from app.schemas.responses import ProviderOption


class RankingEngine:
    """
    Deterministic multi-factor provider ranking engine.
    Supports: price (cheapest), distance (nearest), rating (highest-rated),
    earliest (earliest available slot), and best (configurable composite score).
    Never fabricates missing ratings, distances, or availability.
    """

    def __init__(
        self,
        weight_price: float | None = None,
        weight_distance: float | None = None,
        weight_rating: float | None = None,
        weight_availability: float | None = None,
    ):
        settings = get_settings()
        self.weight_price = weight_price if weight_price is not None else settings.rank_weight_price
        self.weight_distance = (
            weight_distance if weight_distance is not None else settings.rank_weight_distance
        )
        self.weight_rating = weight_rating if weight_rating is not None else settings.rank_weight_rating
        self.weight_availability = (
            weight_availability if weight_availability is not None else settings.rank_weight_availability
        )

    def rank(
        self,
        providers: list[ProviderOption],
        sort_by: str = "best",
        max_distance_km: float | None = None,
    ) -> list[ProviderOption]:
        if not providers:
            return []

        mode = (sort_by or "best").lower()

        # Filter unavailable providers for booking-ready ranking modes
        filtered = [p for p in providers if p.available]
        if max_distance_km is not None:
            filtered = [
                p for p in filtered if p.distance_km is not None and p.distance_km <= max_distance_km
            ]
        if not filtered:
            return []

        if mode == "price":
            ranked = sorted(
                filtered,
                key=lambda p: (
                    p.price,
                    p.distance_km if p.distance_km is not None else float("inf"),
                    -(p.rating if p.rating is not None else -1.0),
                ),
            )
            for idx, item in enumerate(ranked, start=1):
                item.ranking_score = round(1.0 / (idx), 4)
                item.ranking_explanation = (
                    f"Ranked #{idx} by lowest price ({item.currency} {item.price:.0f}, available)."
                )
            return ranked

        if mode == "distance":
            ranked = sorted(
                filtered,
                key=lambda p: (
                    p.distance_km is None,
                    p.distance_km if p.distance_km is not None else float("inf"),
                    p.price,
                ),
            )
            for idx, item in enumerate(ranked, start=1):
                dist_txt = f"{item.distance_km:.2f} km away" if item.distance_km is not None else "distance unavailable"
                item.ranking_score = round(1.0 / idx, 4)
                item.ranking_explanation = f"Ranked #{idx} by nearest distance ({dist_txt})."
            return ranked

        if mode == "rating":
            ranked = sorted(
                filtered,
                key=lambda p: (
                    p.rating is None,
                    -(p.rating if p.rating is not None else -1.0),
                    -p.review_count,
                    p.price,
                ),
            )
            for idx, item in enumerate(ranked, start=1):
                rating_txt = f"{item.rating:.1f}/5 ({item.review_count} reviews)" if item.rating is not None else "no rating yet"
                item.ranking_score = round((item.rating / 5.0) if item.rating is not None else 0.0, 4)
                item.ranking_explanation = f"Ranked #{idx} by customer rating ({rating_txt})."
            return ranked

        if mode == "earliest":
            ranked = sorted(
                filtered,
                key=lambda p: (
                    p.slot_date is None or p.start_time is None,
                    p.slot_date or "9999-12-31",
                    p.start_time or "23:59",
                    p.price,
                ),
            )
            for idx, item in enumerate(ranked, start=1):
                slot_txt = f"{item.slot_date} at {item.start_time}" if item.slot_date and item.start_time else "slot unknown"
                item.ranking_score = round(1.0 / idx, 4)
                item.ranking_explanation = f"Ranked #{idx} by earliest available slot ({slot_txt})."
            return ranked

        # Default: "best" overall match with explicit normalized composite score
        prices = [p.price for p in filtered]
        min_p, max_p = min(prices), max(prices)

        distances = [p.distance_km for p in filtered if p.distance_km is not None]
        min_d, max_d = (min(distances), max(distances)) if distances else (None, None)

        scored: list[tuple[float, ProviderOption]] = []
        for item in filtered:
            factors: list[str] = []
            active_weight = 0.0
            total = 0.0

            # Price score (lower is better)
            price_score = 1.0 if max_p == min_p else (max_p - item.price) / (max_p - min_p)
            total += self.weight_price * price_score
            active_weight += self.weight_price
            factors.append(f"price={item.currency} {item.price:.0f} ({price_score:.2f})")

            # Distance score (only when real distance exists — never fabricated)
            if item.distance_km is not None and min_d is not None and max_d is not None:
                dist_score = 1.0 if max_d == min_d else (max_d - item.distance_km) / (max_d - min_d)
                total += self.weight_distance * dist_score
                active_weight += self.weight_distance
                factors.append(f"distance={item.distance_km:.2f} km ({dist_score:.2f})")

            # Rating score (only when real rating exists — never fabricated)
            if item.rating is not None:
                rating_score = max(0.0, min(1.0, item.rating / 5.0))
                total += self.weight_rating * rating_score
                active_weight += self.weight_rating
                factors.append(f"rating={item.rating:.1f}/5 ({rating_score:.2f})")

            # Availability score
            avail_score = 1.0 if item.available and item.slot_id else 0.0
            total += self.weight_availability * avail_score
            active_weight += self.weight_availability
            factors.append("available=yes")

            final_score = round(total / active_weight, 4) if active_weight > 0 else 0.0
            item.ranking_score = final_score
            item.ranking_explanation = (
                f"Best overall score {final_score:.2f} based on " + ", ".join(factors) + "."
            )
            scored.append((final_score, item))

        scored.sort(key=lambda pair: (-pair[0], pair[1].price))
        return [item for _, item in scored]
