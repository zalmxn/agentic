from typing import Any

from app.config import get_settings
from app.core.ranking_engine import RankingEngine
from app.schemas.responses import ProviderOption
from app.services.backend_client import BackendClient


class SearchCoordinator:
    """Coordinates backend provider searches and deterministic ranking."""

    def __init__(
        self,
        backend_client: BackendClient,
        ranking_engine: RankingEngine | None = None,
    ):
        self.backend_client = backend_client
        self.ranking_engine = ranking_engine or RankingEngine()
        self.max_results = get_settings().max_search_results

    async def search_and_rank(
        self,
        *,
        service_code: str | None,
        location_name: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        date: str | None = None,
        start_time: str | None = None,
        sort_by: str = "best",
        max_price: float | None = None,
        max_distance_km: float | None = None,
        filter_by_location_name: bool = False,
        limit: int | None = None,
        request_id: str | None = None,
    ) -> list[ProviderOption]:
        send_location_name = (
            location_name
            if (filter_by_location_name or latitude is None or longitude is None)
            else None
        )
        payload: dict[str, Any] = {
            "service_code": service_code,
            "location_name": send_location_name,
            "latitude": latitude,
            "longitude": longitude,
            "date": date,
            "start_time": start_time,
            "sort_by": sort_by,
            "max_price": max_price,
            "max_distance_km": max_distance_km,
            "limit": limit or self.max_results,
            "include_unavailable": False,
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        raw = await self.backend_client.search_services(payload, request_id=request_id)
        items = [ProviderOption(**item) for item in raw.get("providers", [])]

        # Graceful fallback: if strict city filter returned 0 matches when user GPS is available,
        # retry coordinate-only search so nearest available providers are still shown.
        if not items and send_location_name and latitude is not None and longitude is not None:
            fallback_payload = {k: v for k, v in payload.items() if k != "location_name"}
            raw = await self.backend_client.search_services(fallback_payload, request_id=request_id)
            items = [ProviderOption(**item) for item in raw.get("providers", [])]

        ranked = self.ranking_engine.rank(
            items,
            sort_by=sort_by,
            max_distance_km=max_distance_km,
        )
        return ranked
