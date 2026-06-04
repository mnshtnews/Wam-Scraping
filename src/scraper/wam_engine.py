"""
src/scraper/wam_engine.py
─────────────────────────
CRASH FIX: استبدال نهج tab-click بـ goto مباشر على URL العربي.

المشكلة كانت:
  - الـ persistent page بتضغط Sports tab
  - في نفس الوقت الـ detail pages بتـcrash الـ browser context كله
  - لأن كل العمليات بتشترك في نفس الـ browser context

الحل:
  - كل subcategory poll بيفتح page جديدة مستقلة
  - goto مباشر على URL مثل https://www.wam.ae/ar/category/football
  - بعد goto ننتظر Angular يـrender
  - بعدها نقفل الـ page فوراً
  - لا persistent state، لا tab-clicks، لا browser crashes
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

# بعد domcontentloaded، ننتظر كده ثواني لـ Angular يـrender الـ cards
_ANGULAR_SETTLE_S = 12.0

# selector المقالات — مؤكد من diagnose3
_CARDS_SELECTOR = ".single-blog-post"
_CARDS_WAIT_MS = 40_000

# ننتظر بعد ظهور الـ cards عشان lazy content يكمل
_SETTLE_AFTER_CARDS_S = 3.0


class WAMScraper:

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browser_manager = BrowserManager(settings)
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
        name = subcategory["name"]
        url = subcategory["url"]   # مثل https://www.wam.ae/ar/category/football

        logger.info(f"Polling WAM subcategory: {name}", url=url)

        listing_html = await self._load_listing_page(url, name)
        if not listing_html:
            logger.error(f"Failed to load WAM subcategory: {name}")
            return []

        stubs = parse_article_list(listing_html, subcategory=name)
        logger.debug(f"Found {len(stubs)} stubs", subcategory=name)

        if not stubs:
            snippet = listing_html[:4000].replace("\n", " ")
            logger.warning(
                f"[SELECTOR_DEBUG] Zero stubs for {name}. HTML snippet:\n{snippet}"
            )
            return []

        new_stubs = [a for a in stubs if a.url not in self._seen_urls]
        logger.info(f"{len(new_stubs)} new articles in {name}")
        if not new_stubs:
            return []

        articles = await self._fetch_articles_batch(new_stubs, batch_size=2)

        for article in articles:
            self._seen_urls.add(article.url)

        return articles

    async def seed_seen_urls(self, known_urls: set[str]) -> None:
        self._seen_urls.update(known_urls)
        logger.info(f"Seeded {len(known_urls)} known URLs into WAM seen set")

    # ── Listing page ──────────────────────────────────────────────────────────

    async def _load_listing_page(self, url: str, name: str) -> Optional[str]:
        """
        فتح صفحة الـ category مباشرةً بـ goto.
        كل محاولة بتفتح page جديدة مستقلة وتقفلها بعد ما نجيب الـ HTML.
        """
        for attempt in range(1, self._settings.max_retries + 1):
            page: Optional[Page] = None
            try:
                page = await self._browser_manager.new_page()

                logger.debug(f"goto {url} (attempt {attempt})")
                await page.goto(
                    url,
                    timeout=self._settings.page_load_timeout,
                    wait_until="domcontentloaded",
                )

                # ننتظر Angular يـrender
                try:
                    await page.wait_for_selector(
                        _CARDS_SELECTOR,
                        timeout=_CARDS_WAIT_MS,
                        state="attached",
                    )
                    logger.debug(f"Cards found for {name}")
                    await asyncio.sleep(_SETTLE_AFTER_CARDS_S)
                except PlaywrightTimeoutError:
                    # مش لازم نفشل — نجرب نجيب الـ HTML زي ما هو
                    logger.warning(
                        f"Cards selector timed out for {name} — "
                        f"proceeding with current DOM. URL: {page.url}"
                    )
                    await asyncio.sleep(_ANGULAR_SETTLE_S)

                html = await page.content()
                logger.debug(f"Got {len(html)} chars for {name}")
                return html

            except Exception as exc:
                logger.warning(f"Error loading {name} (attempt {attempt}): {exc}")
                if "browser" in str(exc).lower() or "target" in str(exc).lower():
                    logger.warning("Browser crash — restarting")
                    await self._browser_manager.restart()

                backoff = min(
                    self._settings.retry_backoff_base * (2 ** (attempt - 1)), 120.0
                )
                logger.info(f"Retrying {name} in {backoff:.0f}s …")
                await asyncio.sleep(backoff)

            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass

        logger.error(f"All attempts failed for {name}")
        return None

    # ── Article detail ────────────────────────────────────────────────────────

    async def _fetch_articles_batch(
        self, stubs: list[RawArticle], batch_size: int = 2
    ) -> list[RawArticle]:
        results: list[RawArticle] = []

        for i in range(0, len(stubs), batch_size):
            batch = stubs[i: i + batch_size]
            tasks = [self._fetch_article_detail(stub) for stub in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for stub, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.warning(f"Failed detail for {stub.url}: {result}")
                    results.append(stub)
                else:
                    results.append(result)  # type: ignore[arg-type]

            if i + batch_size < len(stubs):
                await asyncio.sleep(4.0)

        return results

    async def _fetch_article_detail(self, stub: RawArticle) -> RawArticle:
        html = await self._load_detail_page(stub.url)
        if not html:
            return stub
        return parse_article_detail(html, stub)

    async def _load_detail_page(self, url: str) -> Optional[str]:
        for attempt in range(1, self._settings.max_retries + 1):
            page: Optional[Page] = None
            try:
                page = await self._browser_manager.new_page()
                await page.goto(
                    url,
                    timeout=self._settings.page_load_timeout,
                    wait_until="domcontentloaded",
                )
                for sel in [".blog-content", ".article-body", ".single-blog-post", "article"]:
                    try:
                        await page.wait_for_selector(sel, timeout=15_000, state="attached")
                        break
                    except PlaywrightTimeoutError:
                        continue
                await asyncio.sleep(2.0)
                return await page.content()

            except Exception as exc:
                logger.warning(f"Detail page error {url} (attempt {attempt}): {exc}")
                if "browser" in str(exc).lower() or "target" in str(exc).lower():
                    await self._browser_manager.restart()
                backoff = min(self._settings.retry_backoff_base * (2 ** (attempt - 1)), 60.0)
                await asyncio.sleep(backoff)
            finally:
                if page and not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass

        logger.error(f"All detail attempts failed for {url}")
        return None
