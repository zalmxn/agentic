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

    database_url: str = f"sqlite:///{(BASE_DIR / 'booking_platform.db').as_posix()}"
    jwt_secret_key: str = "dev-local-jwt-secret-change-in-production-32bytes"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    internal_api_token: str = "dev-local-internal-token-change-in-production"
    ai_service_url: str = "http://127.0.0.1:8001"
    ai_request_timeout: float = 15.0
    default_timezone: str = "Asia/Kolkata"
    auto_seed_demo_data: bool = True
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
