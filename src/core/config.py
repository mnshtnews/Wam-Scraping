"""
src/core/config.py
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = Field(default="production")
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=True)

    supabase_url: str
    supabase_service_role_key: str

    telegram_bot_token: str
    telegram_chat_id: str

    @property
    def effective_telegram_bot_token(self) -> str:
        return self.telegram_bot_token

    @property
    def effective_telegram_chat_id(self) -> str:
        return self.telegram_chat_id

    use_openai: bool = Field(default=False)
    openai_api_key: Optional[str] = Field(default=None)
    openai_model: str = Field(default="gpt-4o-mini")

    redis_url: str = Field(default="redis://redis:6379/0")

    wam_base_url: str = Field(default="https://www.wam.ae")
    poll_interval_seconds: int = Field(default=120)
    page_load_timeout: int = Field(default=90_000)
    element_timeout: int = Field(default=60_000)
    max_retries: int = Field(default=3)
    retry_backoff_base: float = Field(default=10.0)

    headless: bool = Field(default=True)
    proxy_url: Optional[str] = Field(default=None)

    sentry_dsn: Optional[str] = Field(default=None)

    @property
    def subcategories(self) -> list[dict]:
        """
        4 subcategories من WAM العربي — مؤكدة من الـ URLs الحقيقية على الموقع.
        """
        return [
            {
                "name": "كرة القدم",
                "slug": "football",
                "category_path": "/ar/category/football",
                "url": "https://www.wam.ae/ar/category/football",
            },
            {
                "name": "فروسية وهجن",
                "slug": "camel-racing",
                "category_path": "/ar/category/camel-racing",
                "url": "https://www.wam.ae/ar/category/camel-racing",
            },
            {
                "name": "رياضات أخرى",
                "slug": "other-sports",
                "category_path": "/ar/category/other-sports",
                "url": "https://www.wam.ae/ar/category/other-sports",
            },
            {
                "name": "رياضة عامة",
                "slug": "sport",
                "category_path": "/ar/category/sport",
                "url": "https://www.wam.ae/ar/category/sport",
            },
        ]

    @field_validator("log_level")
    @classmethod
    def normalise_log_level(cls, v: str) -> str:
        return v.upper()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
