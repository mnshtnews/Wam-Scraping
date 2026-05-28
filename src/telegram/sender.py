"""
src/telegram/sender.py
───────────────────────
Telegram notification module — professional WAM news card format.

Sends a richly-formatted HTML message with:
  • Classification badge (🇦🇪 / 🌍 / 🌐)
  • Subcategory label (كرة القدم / سباقات الخيل والإبل / رياضات أخرى)
  • Article title (bold)
  • Publish date
  • Short summary / excerpt
  • Image (sent as photo with caption when available)
  • Inline keyboard button → full article

Uses aiogram 3.x in non-dispatcher mode (direct Bot API calls).
Rate-limited to 1 message per 2.5s (Telegram allows 30/min).

Mirrors FilGoal's telegram/sender.py quality exactly.
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

# Classification display metadata
_CLASSIFICATION_META = {
    NewsClassification.UAE: {
        "emoji": "🇦🇪",
        "label": "أخبار الإمارات",
    },
    NewsClassification.ARAB: {
        "emoji": "🌍",
        "label": "أخبار عربية",
    },
    NewsClassification.GLOBAL: {
        "emoji": "🌐",
        "label": "أخبار عالمية",
    },
    NewsClassification.UNCLASSIFIED: {
        "emoji": "📰",
        "label": "أخبار رياضية",
    },
}

# Telegram hard limits
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
        self._last_sent: float = 0.0
        self._min_interval: float = 2.5   # max 24 messages/min — safely under Telegram's 30/min

    async def start(self) -> None:
        self._bot = Bot(
            token=self._settings.effective_telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        logger.info("WAM Telegram bot initialised")

    async def stop(self) -> None:
        if self._bot:
            await self._bot.session.close()
        self._bot = None

    async def send(self, article: Article) -> bool:
        """
        Send the article to Telegram.
        Returns True on success, False on failure.
        Tries photo first; falls back to text if photo fails.
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

    # ── Send methods ──────────────────────────────────────────────────────────

    async def _send_photo(self, article: Article) -> bool:
        """Send article as a photo message with caption."""
        caption = self._build_caption(article, limit=_PHOTO_CAPTION_LIMIT)
        keyboard = self._build_keyboard(article)

        try:
            await self._bot.send_photo(  # type: ignore[union-attr]
                chat_id=self._settings.effective_telegram_chat_id,
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
        text = self._build_caption(article, limit=_TEXT_MESSAGE_LIMIT)
        keyboard = self._build_keyboard(article)

        await self._bot.send_message(  # type: ignore[union-attr]
            chat_id=self._settings.effective_telegram_chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        logger.info(f"Telegram text sent: {article.title[:60]}")
        return True

    # ── Message builder ───────────────────────────────────────────────────────

    def _build_caption(self, article: Article, limit: int = 950) -> str:
        """
        Build the Telegram message body.

        Format:
          🇦🇪 أخبار الإمارات  |  كرة القدم

          <b>Title</b>

          Content preview…

          🕐 Date

          اقرأ المقال كاملاً
        """
        meta = _CLASSIFICATION_META[article.classification]
        emoji = meta["emoji"]
        label = meta["label"]

        date_str = self._format_date(article.publish_date)
        summary = self._get_summary(article, max_chars=350)

        lines: list[str] = []

        # Header: classification + subcategory
        lines.append(f"{emoji} <b>{self._escape(label)}</b>  |  {self._escape(article.subcategory)}")
        lines.append("")

        # Title
        lines.append(f"<b>{self._escape(article.title)}</b>")
        lines.append("")

        # Content preview
        if summary:
            lines.append(self._escape(summary))
            lines.append("")

        # Date
        if date_str:
            lines.append(f"🕐 <i>{date_str}</i>")
            lines.append("")

        # Source link
        lines.append(f'<a href="{article.url}">اقرأ المقال كاملاً</a>')

        caption = "\n".join(lines)

        # Safe truncation — preserve source link at end
        if len(caption) > limit:
            caption = caption[: limit - 80].rsplit("\n", 1)[0]
            caption += f'\n\n<a href="{article.url}">اقرأ المقال كاملاً</a>'

        return caption

    def _build_keyboard(self, article: Article) -> InlineKeyboardMarkup:
        """Inline keyboard with a single 'Read full article' button."""
        return InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="📖 اقرأ المقال كاملاً",
                    url=article.url,
                )
            ]]
        )

    # ── Utilities ─────────────────────────────────────────────────────────────

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
    def _get_summary(article: Article, max_chars: int = 350) -> str:
        """Return the best available short text for the notification."""
        text = article.summary or article.content or ""
        if not text:
            return ""
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
