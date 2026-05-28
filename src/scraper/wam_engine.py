"""
src/scraper/wam_engine.py
─────────────────────────
Production WAM scraping engine — modelled on FilGoalScraper.

WAM-specific challenges this engine solves:
  1. Angular SPA — navigating to a subcategory URL opens the homepage first,
     then Angular client-side-routes to the target. We must wait for the
     category content to actually appear, not just domcontentloaded.
  2. Slow cold boot — first page load averages 40s. Subsequent loads are
     faster because the browser context is reused.
  3. Three subcategories — Football, Horse/Camel Racing, Other Sports —
     each polled independently with its own seen-URL tracking.
  4. No RSS — content is rendered dynamically; pure HTTP fetching does not
     work reliably; Playwright is required.

Architecture mirrors FilGoalScraper exactly:
  start() / stop() → __aenter__ / __aexit__
  poll_subcategory(sub) → list[RawArticle]
  seed_seen_urls(known_urls) → None
"""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from src.core.config import Settings
from src.core.models import RawArticle
from src.scraper.browser import BrowserManager
from src.scraper.wam_parser import parse_article_detail, parse_article_list

# ── Angular readiness selectors ───────────────────────────────────────────────
# Tried in order — first match wins. These all indicate the subcategory
# article list has finished rendering.
_CONTENT_READY_SELECTORS = [
    "app-article-item-bottom-text",    # primary Angular component
    ".art-img.single-blog-post",       # rendered card container
    ".single-blog-post",
    "article.blog-post",
    "article",
    "[class*='article-item']",
]

# Maximum ms to wait for Angular to render article cards (generous — WAM is slow)
_CONTENT_WAIT_MS = 60_000

# After Angular renders, wait this long for lazy content to settle
_SETTLE_DELAY_S = 3.0

# Delay between subcategory switches — WAM needs a moment after tab change
_SUBCATEGORY_SWITCH_DELAY_S = 8.0


class WAMScraper:
    """
    Async Playwright scraper for the WAM sports news website.

    Usage::

        async with WAMScraper(settings) as scraper:
            articles = await scraper.poll_subcategory(sub)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browser_manager = BrowserManager(settings)
        # Per-subcategory seen-URL sets (keyed by slug)
        self._seen_urls: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._browser_manager.start()
        logger.info("WAMScraper started")

    async def stop(self) -> None:
        await self._browser_manager.stop()
        logger.info("WAMScraper stopped")

    async def __aenter__(self) -> "WAMScraper":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ── Public interface ──────────────────────────────────────────────────────

    async def poll_subcategory(self, subcategory: dict) -> list[RawArticle]:
        """
        Poll one WAM subcategory and return all new (unseen) RawArticles,
        each enriched with full article body text.

        Mirrors FilGoalScraper.poll_subcategory() exactly in interface.
        """
        name = subcategory["name"]
        url = subcategory["url"]

        logger.info(f"Polling WAM subcategory: {name}", url=url)

        # Step 1 — Load the subcategory listing page
        listing_html = await self._safe_load_listing(url, name)
        if not listing_html:
            logger.error(f"Failed to load WAM listing for {name}")
            return []

        # Step 2 — Parse article stubs from the rendered listing HTML
        stubs = parse_article_list(listing_html, subcategory=name)
        logger.debug(f"Found {len(stubs)} stubs on listing page", subcategory=name)

        # Step 3 — Filter to unseen articles only
        new_stubs = [a for a in stubs if a.url not in self._seen_urls]
        logger.info(f"{len(new_stubs)} new articles in {name}")

        if not new_stubs:
            return []

        # Step 4 — Fetch full article bodies in small concurrent batches
        articles = await self._fetch_articles_batch(new_stubs, batch_size=3)

        # Step 5 — Mark all fetched articles as seen
        for article in articles:
            self._seen_urls.add(article.url)

        return articles

    async def seed_seen_urls(self, known_urls: set[str]) -> None:
        """
        Pre-populate the seen set from persistent storage at startup.
        Prevents re-processing articles that were already sent before a restart.
        Mirrors FilGoalScraper.seed_seen_urls() exactly.
        """
        self._seen_urls.update(known_urls)
        logger.info(f"Seeded {len(known_urls)} known article URLs into WAM seen set")

    # ── Listing page loading ──────────────────────────────────────────────────

    async def _safe_load_listing(self, url: str, name: str) -> Optional[str]:
        """
        Load a WAM subcategory listing page with full retry/timeout handling.
        WAM Angular behaviour: navigating to the URL loads the homepage first,
        then Angular routes to the category. We wait for article content to
        appear, not just for the DOM to load.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._settings.max_retries + 1):
            page: Optional[Page] = None
            try:
                page = await self._browser_manager.new_page()
                html = await self._navigate_listing(page, url)
                return html

            except PlaywrightTimeoutError as exc:
                last_exc = exc
                logger.warning(
                    f"Timeout loading WAM listing {name} "
                    f"(attempt {attempt}/{self._settings.max_retries})"
                )

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"Error loading WAM listing {name} "
                    f"(attempt {attempt}/{self._settings.max_retries}): {exc}"
                )
                if "browser" in str(exc).lower() or "target" in str(exc).lower():
                    logger.warning("Browser crash detected — restarting browser")
                    await self._browser_manager.restart()

            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass

            backoff = min(
                self._settings.retry_backoff_base * (2 ** (attempt - 1)),
                120.0,
            )
            logger.info(f"Retrying WAM listing {name} in {backoff:.0f}s …")
            await asyncio.sleep(backoff)

        logger.error(
            f"All {self._settings.max_retries} attempts failed for WAM listing "
            f"{name}: {last_exc}"
        )
        return None

    async def _navigate_listing(self, page: Page, url: str) -> str:
        """
        Navigate to a WAM subcategory URL and wait for Angular to render content.

        WAM quirk: the URL alone does not open the subcategory — Angular's router
        redirects to the homepage first. We wait for article cards using multiple
        selectors, giving WAM the full generous timeout it needs.
        """
        logger.debug(f"Navigating to WAM URL: {url}")

        # Use domcontentloaded — do NOT wait for networkidle.
        # WAM's Angular app makes many background XHR calls that never fully
        # settle, so networkidle would time out every single time.
        await page.goto(
            url,
            timeout=self._settings.page_load_timeout,
            wait_until="domcontentloaded",
        )

        # Wait for Angular to render article cards
        content_found = await self._wait_for_angular_content(page)

        if content_found:
            # Give Angular a moment to finish rendering any remaining cards
            await asyncio.sleep(_SETTLE_DELAY_S)
        else:
            logger.warning(
                f"Angular content not confirmed for {url} — proceeding with current DOM"
            )

        return await page.content()

    async def _wait_for_angular_content(self, page: Page) -> bool:
        """
        Poll multiple CSS selectors until one matches or timeout expires.
        Returns True if content was confirmed, False if we timed out.

        Tries selectors sequentially — first match wins. This is more reliable
        than waiting for all selectors, because WAM's HTML structure varies
        slightly between subcategories.
        """
        for selector in _CONTENT_READY_SELECTORS:
            try:
                await page.wait_for_selector(
                    selector,
                    timeout=_CONTENT_WAIT_MS,
                    state="attached",
                )
                logger.debug(f"Angular content confirmed (selector: '{selector}')")
                return True
            except PlaywrightTimeoutError:
                continue
            except Exception as exc:
                logger.debug(f"Selector '{selector}' error: {exc}")
                continue

        return False

    # ── Article detail fetching ───────────────────────────────────────────────

    async def _fetch_articles_batch(
        self,
        stubs: list[RawArticle],
        batch_size: int = 3,
    ) -> list[RawArticle]:
        """
        Fetch full article detail pages in small concurrent batches.
        Mirrors FilGoalScraper._fetch_articles_batch() exactly.

        Batching prevents overwhelming WAM's servers and avoids triggering
        rate limiting or bot-detection.
        """
        results: list[RawArticle] = []

        for i in range(0, len(stubs), batch_size):
            batch = stubs[i: i + batch_size]
            tasks = [self._fetch_article_detail(stub) for stub in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for stub, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.warning(
                        f"Failed to fetch WAM article detail for {stub.url}: {result}"
                    )
                    results.append(stub)   # fall back to stub without full body
                else:
                    results.append(result)  # type: ignore[arg-type]

            # Polite inter-batch delay
            if i + batch_size < len(stubs):
                await asyncio.sleep(3.0)

        return results

    async def _fetch_article_detail(self, stub: RawArticle) -> RawArticle:
        """
        Fetch and parse a single WAM article detail page.
        Article pages are simpler Angular views — they render faster than
        the listing pages, so a shorter wait strategy is used.
        """
        html = await self._safe_load_detail(stub.url)
        if not html:
            return stub
        return parse_article_detail(html, stub)

    async def _safe_load_detail(self, url: str) -> Optional[str]:
        """
        Load a WAM article detail page with retry handling.
        Less generous timeout than listing pages — article pages are faster.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._settings.max_retries + 1):
            page: Optional[Page] = None
            try:
                page = await self._browser_manager.new_page()
                html = await self._navigate_detail(page, url)
                return html

            except PlaywrightTimeoutError as exc:
                last_exc = exc
                logger.warning(f"Timeout on WAM article {url} (attempt {attempt})")

            except Exception as exc:
                last_exc = exc
                logger.warning(f"Error on WAM article {url} (attempt {attempt}): {exc}")
                if "browser" in str(exc).lower() or "target" in str(exc).lower():
                    await self._browser_manager.restart()

            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass

            backoff = min(
                self._settings.retry_backoff_base * (2 ** (attempt - 1)),
                60.0,
            )
            await asyncio.sleep(backoff)

        logger.error(f"All attempts failed for WAM article {url}: {last_exc}")
        return None

    async def _navigate_detail(self, page: Page, url: str) -> str:
        """
        Navigate to a WAM article detail page.
        Article pages render faster than listing pages — we wait for the
        article body selector, then a small settle delay.
        """
        await page.goto(
            url,
            timeout=self._settings.page_load_timeout,
            wait_until="domcontentloaded",
        )

        # Try to confirm article body has rendered
        for selector in [".article-body", ".article-content", ".ng-star-inserted p", "article"]:
            try:
                await page.wait_for_selector(
                    selector,
                    timeout=20_000,
                    state="attached",
                )
                break
            except PlaywrightTimeoutError:
                continue

        # Short settle delay for dynamic content
        await asyncio.sleep(2.0)

        return await page.content()
