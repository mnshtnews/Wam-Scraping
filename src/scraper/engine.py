"""
src/scraper/engine.py
─────────────────────
The WAM scraping engine.

Responsibilities:
  • Navigate to each subcategory page
  • Wait intelligently for Angular dynamic content to render
  • Detect new articles since last poll
  • Fetch full article detail pages
  • Yield RawArticle objects to the pipeline
  • Handle all timeouts, crashes, and retries gracefully
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Optional

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from src.core.config import Settings
from src.core.models import RawArticle
from src.scraper.browser import BrowserManager
from src.scraper.parser import parse_article_detail, parse_article_list

# Selectors that indicate the page has finished rendering Angular content
_ANGULAR_READY_SELECTORS = [
    "app-article-item-bottom-text",
    ".single-blog-post",
    "article",
    ".blog-thumbnail",
]

# Time to wait for at least one article to appear (ms)
_CONTENT_WAIT_MS = 45_000


class WAMScraper:
    """
    Async scraper for the WAM sports news website.

    Usage::

        async with WAMScraper(settings) as scraper:
            async for article in scraper.stream_new_articles():
                process(article)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browser_manager = BrowserManager(settings)
        # In-memory seen set — Redis deduplication is the authoritative layer
        self._seen_urls: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._browser_manager.start()

    async def stop(self) -> None:
        await self._browser_manager.stop()

    async def __aenter__(self) -> "WAMScraper":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ── Public interface ──────────────────────────────────────────────────────

    async def poll_subcategory(self, subcategory: dict) -> list[RawArticle]:
        """
        Poll a single subcategory and return all new (unseen) RawArticles.
        Each article includes the full body text fetched from its detail page.
        """
        name = subcategory["name"]
        url = subcategory["url"]

        logger.info(f"Polling subcategory: {name}", url=url)

        # Step 1 — Load listing page
        listing_html = await self._safe_load_page(url, wait_for_content=True)
        if not listing_html:
            logger.error(f"Failed to load listing page for {name}")
            return []

        # Step 2 — Parse article stubs from listing
        stubs = parse_article_list(listing_html, subcategory=name)
        logger.debug(f"Found {len(stubs)} stubs on listing page", subcategory=name)

        # Step 3 — Filter to unseen articles
        new_stubs = [a for a in stubs if a.url not in self._seen_urls]
        logger.info(f"{len(new_stubs)} new articles in {name}")

        if not new_stubs:
            return []

        # Step 4 — Fetch full article details concurrently (batched)
        articles = await self._fetch_articles_batch(new_stubs, batch_size=3)

        # Step 5 — Mark as seen
        for article in articles:
            self._seen_urls.add(article.url)

        return articles

    async def seed_seen_urls(self, known_urls: set[str]) -> None:
        """
        Pre-populate the seen set from persistent storage (called at startup)
        to avoid re-processing already-stored articles after a restart.
        """
        self._seen_urls.update(known_urls)
        logger.info(f"Seeded {len(known_urls)} known article URLs into seen set")

    # ── Page loading ──────────────────────────────────────────────────────────

    async def _safe_load_page(
        self,
        url: str,
        wait_for_content: bool = False,
    ) -> Optional[str]:
        """
        Load a URL with full retry/timeout handling.
        Returns the page HTML or None on failure.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._settings.max_retries + 1):
            page: Optional[Page] = None
            try:
                page = await self._browser_manager.new_page()
                html = await self._navigate_and_extract(page, url, wait_for_content)
                return html

            except PlaywrightTimeoutError as exc:
                last_exc = exc
                logger.warning(
                    f"Timeout loading {url} (attempt {attempt}/{self._settings.max_retries})"
                )

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"Error loading {url} (attempt {attempt}/{self._settings.max_retries}): {exc}"
                )
                # If the browser crashed, restart it
                if "browser" in str(exc).lower() or "target" in str(exc).lower():
                    await self._browser_manager.restart()

            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass

            # Exponential backoff before next attempt
            backoff = min(
                self._settings.retry_backoff_base * (2 ** (attempt - 1)),
                60.0,
            )
            logger.info(f"Retrying {url} in {backoff:.1f}s …")
            await asyncio.sleep(backoff)

        logger.error(f"All {self._settings.max_retries} attempts failed for {url}: {last_exc}")
        return None

    async def _navigate_and_extract(
        self,
        page: Page,
        url: str,
        wait_for_content: bool,
    ) -> str:
        """
        Navigate to a URL and wait for Angular content to be ready.
        Returns the page's outer HTML.
        """
        # Navigate — allow the slow WAM website its time
        await page.goto(
            url,
            timeout=self._settings.page_load_timeout,
            wait_until="domcontentloaded",   # don't wait for all network — WAM is slow
        )

        if wait_for_content:
            # Wait for Angular to render article cards
            await self._wait_for_angular_content(page)
        else:
            # For article detail pages, wait a bit for body to render
            await asyncio.sleep(3)
            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=self._settings.page_load_timeout,
                )
            except PlaywrightTimeoutError:
                logger.debug(f"networkidle timeout for {url} — proceeding with current DOM")

        return await page.content()

    async def _wait_for_angular_content(self, page: Page) -> None:
        """
        Wait until Angular has rendered at least one article card.
        Tries multiple selectors with a generous timeout.
        """
        for selector in _ANGULAR_READY_SELECTORS:
            try:
                await page.wait_for_selector(
                    selector,
                    timeout=_CONTENT_WAIT_MS,
                    state="attached",
                )
                logger.debug(f"Angular content ready (matched: {selector})")
                # Extra small delay to let adjacent items finish rendering
                await asyncio.sleep(2)
                return
            except PlaywrightTimeoutError:
                continue

        # If no selector matched, log a warning and proceed anyway —
        # the page may still contain parseable content
        logger.warning("Could not confirm Angular render — proceeding with current DOM")

    # ── Batch article fetching ────────────────────────────────────────────────

    async def _fetch_articles_batch(
        self,
        stubs: list[RawArticle],
        batch_size: int = 3,
    ) -> list[RawArticle]:
        """
        Fetch full article details in small concurrent batches to avoid
        overwhelming the slow WAM server.
        """
        results: list[RawArticle] = []

        for i in range(0, len(stubs), batch_size):
            batch = stubs[i : i + batch_size]
            tasks = [self._fetch_article_detail(stub) for stub in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for stub, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.warning(f"Failed to fetch detail for {stub.url}: {result}")
                    results.append(stub)   # use stub without full content
                else:
                    results.append(result)  # type: ignore[arg-type]

            # Polite delay between batches
            await asyncio.sleep(2)

        return results

    async def _fetch_article_detail(self, stub: RawArticle) -> RawArticle:
        """Fetch and parse a single article detail page."""
        html = await self._safe_load_page(stub.url, wait_for_content=False)
        if not html:
            return stub
        return parse_article_detail(html, stub)
