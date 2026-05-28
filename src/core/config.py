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

    # ── OpenAI ───────────────────────────────────────────────────────────────
    # Set USE_OPENAI=true in .env to enable GPT-4o-mini as classification fallback.
    # Set USE_OPENAI=false (default) to rely only on keyword + spaCy — zero API cost.
    use_openai: bool = Field(default=False)
    openai_api_key: Optional[str] = Field(default=None)
    openai_model: str = Field(default="gpt-4o-mini")

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = Field(default="redis://redis:6379/0")

    # ── Scraper ───────────────────────────────────────────────────────────────
    wam_base_url: str = Field(default="https://www.wam.ae")
    wam_sports_url: str = Field(default="https://www.wam.ae/en/category/sport")
    poll_interval_seconds: int = Field(default=30)
    page_load_timeout: int = Field(default=60_000)  # ms
    element_timeout: int = Field(default=30_000)    # ms
    max_retries: int = Field(default=5)
    retry_backoff_base: float = Field(default=5.0)

    # ── Browser ───────────────────────────────────────────────────────────────
    headless: bool = Field(default=True)
    proxy_url: Optional[str] = Field(default=None)

    # ── Sentry ────────────────────────────────────────────────────────────────
    sentry_dsn: Optional[str] = Field(default=None)

    # ── Derived ───────────────────────────────────────────────────────────────
    @property
    def subcategories(self) -> list[dict]:
        """
        WAM Sports RSS feed definitions.
        Using RSS instead of HTML scraping — bypasses F5 bot protection.
        """
        return [
            {
                "name": "Sports",
                "slug": "sports",
                "url": "https://www.wam.ae/en/rss/sports",
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