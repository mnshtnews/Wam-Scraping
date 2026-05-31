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
    poll_interval_seconds: int = Field(default=120)
    page_load_timeout: int = Field(default=90_000)
    element_timeout: int = Field(default=60_000)
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
        WAM Sports subcategories — confirmed from live site (diagnose3.py).

        Navigation flow (required — direct URL goto does NOT work):
          1. Load https://www.wam.ae/en  (English homepage)
          2. Click the "Sports" tab
          3. Click the subcategory link matching `category_path`

        `category_path` values confirmed from Step 8 of diagnose3.py:
          /en/category/football     → كرة قدم
          /en/category/other-sports → رياضات أخرى
          /en/category/sport        → كل الرياضة (includes Equestrian)

        Note: Equestrian / Camel Racing has no dedicated nav link on the
        current WAM site — it is covered by the general sport feed.
        """
        return [
            {
                "name": "كرة القدم",
                "slug": "football",
                "category_path": "/en/category/football",
            },
            {
                "name": "رياضات أخرى",
                "slug": "other-sports",
                "category_path": "/en/category/other-sports",
            },
            {
                "name": "رياضة عامة",
                "slug": "sport",
                "category_path": "/en/category/sport",
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
