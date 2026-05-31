"""
src/scraper/wam_engine.py
─────────────────────────
Production WAM scraping engine — fully rewritten based on live site investigation.

ROOT CAUSE (confirmed by three diagnostic runs):

  PROBLEM 1 — Wrong navigation strategy.
    The old code did page.goto("https://www.wam.ae/en/sports/football").
    WAM's Angular router ignores that path and always lands on the homepage.
    The page title came back as "Home | " (or "الرئيسية | ") on every URL.
    No article cards ever appeared because the subcategory never loaded.

  PROBLEM 2 — Wrong base URL / language.
    WAM detects the browser locale and redirects to /ar/ (Arabic) by default.
    All article links in the Arabic version use /ar/category/football, not
    /en/sports/football.  We must force the English version explicitly.

  CORRECT NAVIGATION FLOW (confirmed by diagnose3.py):
    1. goto("https://www.wam.ae/en")          ← force English homepage
    2. wait for ant-tabs to render
    3. click the "Sports" tab (text match — don't rely on index)
    4. wait for .single-blog-post to appear   ← 32 cards confirmed
    5. (optional) click Football / Other sub-filter link
    6. capture page.content() and parse

  CORRECT SELECTORS (confirmed from real rendered HTML):
    Cards:    .single-blog-post  OR  app-article-item-bottom-text
    Title:    a.post-title
    Image:    .blog-thumbnail img  OR  a > img
    Date:     .post-date  OR  span.text-muted small
    URL:      a[href*='/en/article/']
    Pattern:  /en/article/<alphanumeric-id>-<slug>

  SUBCATEGORY URLS (confirmed from Step 8 links, English equivalents):
    All Sports:   /en/category/sport
    Football:     /en/category/football
    Other Sports: /en/category/other-sports
    (Equestrian is not a separate link — it's included in "All Sports")
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

# ── Navigation constants ───────────────────────────────────────────────────────
# Always start from the English homepage — WAM redirects /ar/ by default
WAM_EN_HOME = "https://www.wam.ae/ar"

# The Sports tab text in English (used for text-based tab matching)
_SPORTS_TAB_TEXT = "Sports"

# After clicking a tab or filter, wait this long for Angular to re-render
_TAB_CLICK_SETTLE_S = 6.0

# Wait this long for the article cards selector to appear
_CARDS_WAIT_MS = 45_000

# Primary selector — confirmed present (32 elements) after Sports tab click
_CARDS_READY_SELECTOR = ".single-blog-post"

# Secondary ready selector
_CARDS_READY_SELECTOR_ALT = "app-article-item-bottom-text"

# After cards appear, wait for lazy images / remaining cards to load
_SETTLE_DELAY_S = 3.0


class WAMScraper:
    """
    Async Playwright scraper for the WAM sports news website.

    Navigation model (confirmed from live site):
      - WAM is a full Angular SPA.  Direct URL navigation always lands on
        the homepage regardless of the path in the URL.
      - Correct flow: load /en homepage → click "Sports" tab → optionally
        click a subcategory filter link → read rendered HTML.
      - A single persistent page is reused across subcategory polls to avoid
        re-loading the entire SPA each time.

    Usage::

        async with WAMScraper(settings) as scraper:
            articles = await scraper.poll_subcategory(sub)
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browser_manager = BrowserManager(settings)
        self._seen_urls: set[str] = set()
        # Persistent page reused across polls — avoids full SPA cold-boot each time
        self._page: Optional[Page] = None
        self._sports_loaded: bool = False   # True once Sports tab has been clicked

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._browser_manager.start()
        logger.info("WAMScraper started")

    async def stop(self) -> None:
        if self._page and not self._page.is_closed():
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None
        self._sports_loaded = False
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
        Poll one WAM subcategory and return all new (unseen) RawArticles.

        Navigation flow per poll:
          1. Ensure a live page exists (create + navigate to /en if needed)
          2. Click the Sports top-level tab (if not already on Sports)
          3. Click the subcategory filter link (football / other-sports)
          4. Parse rendered HTML for article cards
          5. Fetch full article bodies for new articles
        """
        name = subcategory["name"]
        category_path = subcategory["category_path"]   # e.g. /en/category/football

        logger.info(f"Polling WAM subcategory: {name}", category_path=category_path)

        # Step 1-3: navigate to the subcategory
        listing_html = await self._load_subcategory(name, category_path)
        if not listing_html:
            logger.error(f"Failed to load WAM subcategory: {name}")
            return []

        # Step 4: parse article stubs
        stubs = parse_article_list(listing_html, subcategory=name)
        logger.debug(f"Found {len(stubs)} stubs", subcategory=name)

        if not stubs:
            # Emit first 4 kB of HTML to make selector failures self-diagnosing
            snippet = listing_html[:4000].replace("\n", " ")
            logger.warning(
                f"[SELECTOR_DEBUG] Zero stubs for {name}. "
                f"HTML snippet:\n{snippet}"
            )
            return []

        # Step 5: filter to unseen
        new_stubs = [a for a in stubs if a.url not in self._seen_urls]
        logger.info(f"{len(new_stubs)} new articles in {name}")
        if not new_stubs:
            return []

        # Step 6: fetch full article bodies
        articles = await self._fetch_articles_batch(new_stubs, batch_size=3)

        for article in articles:
            self._seen_urls.add(article.url)

        return articles

    async def seed_seen_urls(self, known_urls: set[str]) -> None:
        self._seen_urls.update(known_urls)
        logger.info(f"Seeded {len(known_urls)} known URLs into WAM seen set")

    # ── SPA navigation ────────────────────────────────────────────────────────

    async def _get_page(self) -> Page:
        """
        Return the persistent page, creating and navigating it if needed.
        If the page has crashed or closed, recreates it from scratch.
        """
        if self._page and not self._page.is_closed():
            return self._page

        # Page is dead — recreate
        self._sports_loaded = False
        logger.info("Creating new browser page and loading WAM Arabic homepage")
        self._page = await self._browser_manager.new_page()

        # Navigate to the English homepage explicitly.
        # Do NOT use /en/sports/football — Angular ignores the path on cold load.
        await self._page.goto(
            WAM_EN_HOME,
            timeout=self._settings.page_load_timeout,
            wait_until="domcontentloaded",
        )

        # Wait for the top-level tabs to appear (proves Angular has bootstrapped)
        try:
            await self._page.wait_for_selector(
                ".ant-tabs-tab",
                timeout=_CARDS_WAIT_MS,
                state="attached",
            )
            logger.info(f"WAM homepage loaded: {self._page.url}")
        except PlaywrightTimeoutError:
            logger.warning("Tabs did not appear — proceeding anyway")

        return self._page

    async def _ensure_sports_tab(self, page: Page) -> bool:
        """
        Click the 'Sports' top-level tab if we're not already on the Sports view.
        Returns True if Sports content confirmed, False otherwise.
        """
        if self._sports_loaded:
            return True

        logger.info("Clicking 'Sports' tab")

        # Find tab by text content — more robust than index (index varies by locale)
        tabs = await page.query_selector_all(".ant-tabs-tab")
        sports_tab = None
        for tab in tabs:
            text = (await tab.inner_text()).strip().lower()
            if "sport" in text or "رياض" in text:   # English or Arabic label
                sports_tab = tab
                break

        if not sports_tab:
            logger.error(
                f"Could not find Sports tab among {len(tabs)} tabs. "
                f"Tab texts: {[(await t.inner_text()).strip() for t in tabs]}"
            )
            return False

        await sports_tab.click()
        await asyncio.sleep(_TAB_CLICK_SETTLE_S)

        # Confirm article cards appeared
        try:
            await page.wait_for_selector(
                _CARDS_READY_SELECTOR,
                timeout=_CARDS_WAIT_MS,
                state="attached",
            )
            self._sports_loaded = True
            logger.info(
                f"Sports tab loaded — URL: {page.url}, title: {await page.title()}"
            )
            return True
        except PlaywrightTimeoutError:
            logger.warning(
                f"Article cards did not appear after Sports tab click. "
                f"URL: {page.url}"
            )
            return False

    async def _load_subcategory(
        self, name: str, category_path: str
    ) -> Optional[str]:
        """
        Navigate to a WAM subcategory and return the rendered HTML.

        Strategy:
          - Reuse the persistent page (avoids full SPA cold-boot each poll)
          - Ensure Sports tab is active
          - Click the subcategory filter link (e.g. /en/category/football)
          - Wait for article cards to confirm render
          - Return page.content()

        On any failure, falls back to full page reload.
        """
        for attempt in range(1, self._settings.max_retries + 1):
            try:
                page = await self._get_page()

                # Ensure we're on the Sports tab
                sports_ok = await self._ensure_sports_tab(page)
                if not sports_ok:
                    logger.warning("Sports tab not confirmed — forcing full reload")
                    await self._hard_reset()
                    continue

                # Click the subcategory filter link
                logger.debug(
                    f"Clicking subcategory link: {category_path}", subcategory=name
                )
                clicked = await self._click_category_link(page, category_path)

                if clicked:
                    await asyncio.sleep(_TAB_CLICK_SETTLE_S)
                    # Wait for cards to confirm render
                    try:
                        await page.wait_for_selector(
                            _CARDS_READY_SELECTOR,
                            timeout=_CARDS_WAIT_MS,
                            state="attached",
                        )
                    except PlaywrightTimeoutError:
                        logger.warning(
                            f"Cards not confirmed after subcategory click for {name}"
                        )

                await asyncio.sleep(_SETTLE_DELAY_S)
                html = await page.content()
                logger.debug(
                    f"Captured {len(html)} chars for {name} "
                    f"(attempt {attempt})"
                )
                return html

            except Exception as exc:
                logger.warning(
                    f"Error loading {name} (attempt {attempt}): {exc}"
                )
                if "browser" in str(exc).lower() or "target" in str(exc).lower():
                    logger.warning("Browser crash detected — restarting")
                    await self._hard_reset()

                backoff = min(
                    self._settings.retry_backoff_base * (2 ** (attempt - 1)),
                    120.0,
                )
                logger.info(f"Retrying {name} in {backoff:.0f}s …")
                await asyncio.sleep(backoff)

        logger.error(f"All attempts failed for {name}")
        return None

    async def _click_category_link(self, page: Page, category_path: str) -> bool:
        """
        Find and click a category link matching category_path.
        WAM renders subcategory links as <a href="/en/category/football"> etc.
        Returns True if the link was found and clicked.
        """
        # Try exact href match first
        selector = f'a[href="{category_path}"]'
        try:
            link = await page.query_selector(selector)
            if link:
                await link.click()
                logger.debug(f"Clicked category link: {category_path}")
                return True
        except Exception as exc:
            logger.debug(f"Could not click {selector}: {exc}")

        # Try partial href match (handles trailing slashes etc.)
        slug = category_path.rstrip("/").split("/")[-1]
        selector_partial = f'a[href*="{slug}"]'
        try:
            links = await page.query_selector_all(selector_partial)
            for link in links:
                href = await link.get_attribute("href") or ""
                # Exclude the parent sport category link when we want a sub
                if slug in href and href != "/ar/category/sport":
                    await link.click()
                    logger.debug(f"Clicked partial category link: {href}")
                    return True
        except Exception as exc:
            logger.debug(f"Partial link click failed: {exc}")

        logger.warning(
            f"Could not find category link for path: {category_path}. "
            f"Proceeding with current page content."
        )
        return False

    async def _hard_reset(self) -> None:
        """Close the current page and clear state — forces full reload next call."""
        logger.warning("Hard-resetting browser page")
        if self._page and not self._page.is_closed():
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None
        self._sports_loaded = False
        # Brief pause before recreating
        await asyncio.sleep(5.0)

    # ── Article detail fetching ───────────────────────────────────────────────

    async def _fetch_articles_batch(
        self, stubs: list[RawArticle], batch_size: int = 3
    ) -> list[RawArticle]:
        """Fetch full article detail pages in small concurrent batches."""
        results: list[RawArticle] = []

        for i in range(0, len(stubs), batch_size):
            batch = stubs[i: i + batch_size]
            tasks = [self._fetch_article_detail(stub) for stub in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for stub, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.warning(
                        f"Failed to fetch article detail for {stub.url}: {result}"
                    )
                    results.append(stub)
                else:
                    results.append(result)  # type: ignore[arg-type]

            if i + batch_size < len(stubs):
                await asyncio.sleep(3.0)

        return results

    async def _fetch_article_detail(self, stub: RawArticle) -> RawArticle:
        """Fetch and parse a single WAM article detail page via a separate page."""
        html = await self._safe_load_detail(stub.url)
        if not html:
            return stub
        return parse_article_detail(html, stub)

    async def _safe_load_detail(self, url: str) -> Optional[str]:
        """Load an article detail page on a temporary new page (not the main page)."""
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._settings.max_retries + 1):
            detail_page: Optional[Page] = None
            try:
                detail_page = await self._browser_manager.new_page()
                await detail_page.goto(
                    url,
                    timeout=self._settings.page_load_timeout,
                    wait_until="domcontentloaded",
                )
                # Wait for article body
                for selector in [
                    ".article-body", ".article-content",
                    ".single-blog-post", ".blog-content",
                    ".ng-star-inserted p", "article",
                ]:
                    try:
                        await detail_page.wait_for_selector(
                            selector, timeout=15_000, state="attached"
                        )
                        break
                    except PlaywrightTimeoutError:
                        continue

                await asyncio.sleep(2.0)
                return await detail_page.content()

            except PlaywrightTimeoutError as exc:
                last_exc = exc
                logger.warning(f"Timeout on article {url} (attempt {attempt})")
            except Exception as exc:
                last_exc = exc
                logger.warning(f"Error on article {url} (attempt {attempt}): {exc}")
                if "browser" in str(exc).lower() or "target" in str(exc).lower():
                    await self._browser_manager.restart()
            finally:
                if detail_page and not detail_page.is_closed():
                    try:
                        await detail_page.close()
                    except Exception:
                        pass

            backoff = min(
                self._settings.retry_backoff_base * (2 ** (attempt - 1)), 60.0
            )
            await asyncio.sleep(backoff)

        logger.error(f"All attempts failed for article {url}: {last_exc}")
        return None
