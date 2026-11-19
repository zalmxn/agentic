from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backend_url: str = "http://127.0.0.1:8000"
    internal_api_token: str = "dev-local-internal-token-change-in-production"
    request_timeout: float = 10.0
    default_timezone: str = "Asia/Kolkata"
    conversation_ttl_minutes: int = 30
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60
    max_message_length: int = 1000
    max_search_results: int = 10
    log_level: str = "INFO"

    # Configurable composite ranking weights for "best overall match"
    rank_weight_price: float = 0.35
    rank_weight_distance: float = 0.25
    rank_weight_rating: float = 0.25
    rank_weight_availability: float = 0.15


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
