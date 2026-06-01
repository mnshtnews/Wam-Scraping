"""
src/core/pipeline.py
─────────────────────
FIX 1: On first run, only accept articles published AFTER startup time.
NOTE:  No translation needed — WAM Arabic version (/ar/) is scraped directly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from src.classifier.engine import ArticleClassifier
from src.core.config import Settings
from src.core.models import Article, RawArticle, ScrapingStatus
from src.database.cache import DeduplicationCache
from src.database.repository import ArticleRepository
from src.scraper.wam_engine import WAMScraper
from src.telegram.sender import TelegramSender


class ArticlePipeline:

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._scraper = WAMScraper(settings)
        self._classifier = ArticleClassifier(settings)
        self._repository = ArticleRepository(settings)
        self._cache = DeduplicationCache(settings)
        self._telegram = TelegramSender(settings)
        self._running = False
        # FIX 1: record exact startup time — reject anything older
        self._startup_time: datetime = datetime.now(timezone.utc)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("Starting ArticlePipeline …")
        await self._repository.connect()
        await self._cache.connect()
        await self._telegram.start()
        await self._scraper.start()

        existing_hashes = await self._repository.get_all_hashes()
        existing_urls = await self._repository.get_all_urls()

        await self._cache.bulk_mark_seen(existing_hashes)
        await self._scraper.seed_seen_urls(existing_urls)

        self._running = True
        logger.info(
            f"ArticlePipeline started ✓  (startup_time={self._startup_time.isoformat()})"
        )

    async def stop(self) -> None:
        self._running = False
        await self._scraper.stop()
        await self._telegram.stop()
        await self._repository.disconnect()
        await self._cache.disconnect()
        logger.info("ArticlePipeline stopped")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        logger.info(
            f"Monitoring {len(self._settings.subcategories)} WAM subcategories "
            f"with {self._settings.poll_interval_seconds}s sleep between cycles",
            subcategories=[s["name"] for s in self._settings.subcategories],
        )

        while self._running:
            for sub in self._settings.subcategories:
                if not self._running:
                    break
                try:
                    await self._poll_subcategory(sub)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"Unhandled error polling WAM {sub['name']}: {exc}")

            logger.debug(
                f"WAM poll cycle complete — sleeping {self._settings.poll_interval_seconds}s"
            )
            await asyncio.sleep(self._settings.poll_interval_seconds)

    async def run_once(self) -> list[Article]:
        all_articles: list[Article] = []
        for sub in self._settings.subcategories:
            articles = await self._poll_subcategory(sub)
            all_articles.extend(articles)
        return all_articles

    # ── Per-subcategory poll ──────────────────────────────────────────────────

    async def _poll_subcategory(self, sub: dict) -> list[Article]:
        raw_articles = await self._scraper.poll_subcategory(sub)
        if not raw_articles:
            return []

        processed: list[Article] = []
        for raw in raw_articles:
            article = await self._process_article(raw)
            if article:
                processed.append(article)

        return processed

    # ── Per-article processing ────────────────────────────────────────────────

    async def _process_article(self, raw: RawArticle) -> Optional[Article]:

        # ── FIX 1: reject articles older than startup time ────────────────────
        if raw.publish_date:
            pub = raw.publish_date
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            if pub < self._startup_time:
                logger.debug(
                    f"Skipping old article (published {pub.isoformat()}): {raw.url}"
                )
                return None

        # ── Deduplication ─────────────────────────────────────────────────────
        if await self._cache.is_seen(raw.article_hash):
            logger.debug(f"Already in cache, skipping: {raw.url}")
            return None

        if await self._repository.exists_by_hash(raw.article_hash):
            await self._cache.mark_seen(raw.article_hash)
            logger.debug(f"Already in DB, skipping: {raw.url}")
            return None

        # ── Classify ──────────────────────────────────────────────────────────
        classification_result = await self._classifier.classify(raw)

        # ── Build Article ─────────────────────────────────────────────────────
        article = Article(
            **raw.model_dump(),
            classification=classification_result.classification,
            classification_confidence=classification_result.confidence,
            classification_method=classification_result.method,
            detected_uae_entities=classification_result.uae_entities,
            detected_arab_entities=classification_result.arab_entities,
            detected_global_entities=classification_result.global_entities,
            status=ScrapingStatus.CLASSIFIED,
            scraped_at=datetime.utcnow(),
        )

        # ── Persist ───────────────────────────────────────────────────────────
        saved_row = await self._repository.save(article)
        if not saved_row:
            return None

        await self._cache.mark_seen(article.article_hash)

        # ── Send to Telegram ──────────────────────────────────────────────────
        sent = await self._telegram.send(article)
        if sent:
            await self._repository.update_telegram_sent(article.article_hash)
            article = article.model_copy(
                update={
                    "telegram_sent": True,
                    "telegram_sent_at": datetime.utcnow(),
                    "status": ScrapingStatus.PUBLISHED,
                }
            )

        logger.info(
            "WAM article pipeline complete",
            title=article.title[:60],
            classification=article.classification.value,
            subcategory=article.subcategory,
            telegram_sent=sent,
        )

        return article
