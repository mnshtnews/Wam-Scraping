"""
src/telegram/sender.py
───────────────────────
Telegram notification module.

Sends a richly-formatted message with:
  • Classification badge (🇦🇪 / 🌍 / 🌐)
  • Article title (bold)
  • Publish date
  • Short summary / excerpt
  • Image (sent as photo with caption)
  • Source link button

Uses aiogram 3.x in non-dispatcher mode (direct Bot API calls)
for simplicity inside an async context.
"""

from __future__ import annotations

import asyncio
import textwrap
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from src.core.config import Settings
from src.core.models import Article, NewsClassification

# Classification display config
_CLASSIFICATION_META = {
    NewsClassification.UAE: {
        "emoji": "🇦🇪",
        "label": "UAE News",
        "color_tag": "#00732F",
    },
    NewsClassification.ARAB: {
        "emoji": "🌍",
        "label": "Arab News",
        "color_tag": "#006C35",
    },
    NewsClassification.GLOBAL: {
        "emoji": "🌐",
        "label": "Global News",
        "color_tag": "#0047AB",
    },
    NewsClassification.UNCLASSIFIED: {
        "emoji": "❓",
        "label": "Unclassified",
        "color_tag": "#888888",
    },
}

# Telegram caption hard limit (1024 chars for photos, 4096 for text)
_PHOTO_CAPTION_LIMIT = 950
_TEXT_MESSAGE_LIMIT = 3800


class TelegramSender:
    """
    Sends article notifications to a Telegram chat.

    Usage::

        sender = TelegramSender(settings)
        await sender.start()
        await sender.send(article)
        await sender.stop()
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bot: Optional[Bot] = None
        # Rate limiting: max 1 message per 2 seconds (Telegram limit: 30/min)
        self._last_sent: float = 0.0
        self._min_interval: float = 2.5

    async def start(self) -> None:
        self._bot = Bot(
            token=self._settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        logger.info("Telegram bot initialised")

    async def stop(self) -> None:
        if self._bot:
            await self._bot.session.close()
        self._bot = None

    async def send(self, article: Article) -> bool:
        """
        Send the article to Telegram.
        Returns True on success, False on failure.
        """
        if not self._bot:
            logger.error("TelegramSender not started")
            return False

        await self._rate_limit()

        try:
            if article.image_url:
                return await self._send_photo(article)
            else:
                return await self._send_text(article)
        except Exception as exc:
            logger.error(f"Telegram send failed for {article.url}: {exc}")
            return False

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _send_photo(self, article: Article) -> bool:
        """Send article as a photo message with caption."""
        caption = self._build_caption(article, limit=_PHOTO_CAPTION_LIMIT)
        keyboard = self._build_keyboard(article)

        try:
            await self._bot.send_photo(  # type: ignore[union-attr]
                chat_id=self._settings.telegram_chat_id,
                photo=article.image_url,
                caption=caption,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            logger.info(f"Telegram photo sent: {article.title[:60]}")
            return True
        except Exception as exc:
            logger.warning(f"Photo send failed ({exc}), falling back to text")
            return await self._send_text(article)

    async def _send_text(self, article: Article) -> bool:
        """Send article as a text message."""
        text = self._build_text_message(article)
        keyboard = self._build_keyboard(article)

        await self._bot.send_message(  # type: ignore[union-attr]
            chat_id=self._settings.telegram_chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        logger.info(f"Telegram text sent: {article.title[:60]}")
        return True

    def _build_caption(self, article: Article, limit: int = 950) -> str:
        """Build a concise photo caption (≤ limit chars)."""
        meta = _CLASSIFICATION_META[article.classification]
        emoji = meta["emoji"]
        label = meta["label"]

        date_str = self._format_date(article.publish_date)
        summary = self._get_summary(article, max_chars=300)

        caption = (
            f"{emoji} <b>{label}</b>  |  {article.subcategory}\n\n"
            f"<b>{self._escape(article.title)}</b>\n\n"
        )

        if date_str:
            caption += f"🕐 {date_str}\n\n"

        if summary:
            caption += f"{self._escape(summary)}\n\n"

        caption += f"📰 <a href='{article.url}'>Read full article</a>"

        # Truncate if needed
        if len(caption) > limit:
            caption = caption[: limit - 30] + "…\n\n📰 <a href='{article.url}'>Read more</a>"

        return caption

    def _build_text_message(self, article: Article) -> str:
        """Build a full text message."""
        meta = _CLASSIFICATION_META[article.classification]
        emoji = meta["emoji"]
        label = meta["label"]

        date_str = self._format_date(article.publish_date)
        summary = self._get_summary(article, max_chars=600)

        parts = [
            f"{emoji} <b>{label}</b>  |  {article.subcategory}\n",
            f"<b>{self._escape(article.title)}</b>\n",
        ]

        if date_str:
            parts.append(f"🕐 {date_str}\n")

        if summary:
            parts.append(f"\n{self._escape(summary)}\n")

        parts.append(f"\n🔗 {article.url}")

        return "\n".join(parts)[:_TEXT_MESSAGE_LIMIT]

    def _build_keyboard(self, article: Article) -> InlineKeyboardMarkup:
        """Build an inline keyboard with a 'Read Article' button."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📖 Read Full Article",
                        url=article.url,
                    )
                ]
            ]
        )

    async def _rate_limit(self) -> None:
        """Enforce minimum interval between Telegram messages."""
        import time
        elapsed = time.monotonic() - self._last_sent
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_sent = time.monotonic()

    @staticmethod
    def _format_date(dt: Optional[datetime]) -> str:
        if not dt:
            return ""
        return dt.strftime("%d %b %Y, %H:%M UTC")

    @staticmethod
    def _get_summary(article: Article, max_chars: int = 400) -> str:
        """Return the best available short text for the notification."""
        text = article.summary or article.content or ""
        if not text:
            return ""
        # Take first paragraph / first max_chars chars
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        first = paragraphs[0] if paragraphs else text
        return textwrap.shorten(first, width=max_chars, placeholder="…")

    @staticmethod
    def _escape(text: str) -> str:
        """Escape HTML special characters for Telegram HTML parse mode."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )