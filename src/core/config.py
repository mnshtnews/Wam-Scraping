"""
src/core/config.py
──────────────────
Centralised configuration using Pydantic-Settings.
All values are loaded from environment variables / .env file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings — loaded once, shared everywhere."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Environment ──────────────────────────────────────────────────────────
    env: str = Field(default="production")
    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=True)

    # ── Supabase ─────────────────────────────────────────────────────────────
    supabase_url: str
    supabase_service_role_key: str

    # ── Telegram ─────────────────────────────────────────────────────────────
    telegram_bot_token: str
    telegram_chat_id: str

    @property
    def effective_telegram_bot_token(self) -> str:
        return self.telegram_bot_token

    @property
    def effective_telegram_chat_id(self) -> str:
        return self.telegram_chat_id

    # ── OpenAI ───────────────────────────────────────────────────────────────
    use_openai: bool = Field(default=False)
    openai_api_key: Optional[str] = Field(default=None)
    openai_model: str = Field(default="gpt-4o-mini")

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://redis:6379/0")

    # ── Scraper ───────────────────────────────────────────────────────────────
    wam_base_url: str = Field(default="https://www.wam.ae")
    poll_interval_seconds: int = Field(default=120)   # 2 min — WAM is slow to update
    page_load_timeout: int = Field(default=90_000)    # ms — 90s for Angular cold boot
    element_timeout: int = Field(default=60_000)      # ms — 60s for subcategory content
    max_retries: int = Field(default=3)
    retry_backoff_base: float = Field(default=10.0)

    # ── Browser ───────────────────────────────────────────────────────────────
    headless: bool = Field(default=True)
    proxy_url: Optional[str] = Field(default=None)

    # ── Sentry ────────────────────────────────────────────────────────────────
    sentry_dsn: Optional[str] = Field(default=None)

    # ── WAM subcategories ─────────────────────────────────────────────────────
    @property
    def subcategories(self) -> list[dict]:
        """
        WAM Sports has 3 subcategories navigated via the top tab bar.

        Important: WAM is Angular-based. Navigating directly to a category
        URL opens the homepage first, then auto-routes to the category.
        The WAMScraper handles this correctly — these URLs are the canonical
        deep-link targets that Angular eventually renders.
        """
        return [
            {
                "name": "كرة القدم",
                "slug": "football",
                "url": "https://www.wam.ae/en/sports/football",
                "tab_index": 0,
            },
            {
                "name": "سباقات الخيل والإبل",
                "slug": "equestrian-camel-racing",
                "url": "https://www.wam.ae/en/sports/equestrian-camel-racing",
                "tab_index": 1,
            },
            {
                "name": "رياضات أخرى",
                "slug": "other-sports",
                "url": "https://www.wam.ae/en/sports/other-sports",
                "tab_index": 2,
            },
        ]

    @field_validator("log_level")
    @classmethod
    def normalise_log_level(cls, v: str) -> str:
        return v.upper()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
