"""
src/scraper/wam_rss_engine.py
──────────────────────────────
HTTP-based scraper for WAM (wam.ae) sports news.

WAM returns HTML with articles embedded in JSON-LD (application/ld+json).
We parse the JSON-LD articleBody which contains all article summaries,
then fetch each article's full page for details.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from src.core.config import Settings
from src.core.models import RawArticle

WAM_BASE = "https://www.wam.ae"

WAM_FEEDS = [
    {"name": "Sports", "url": "https://www.wam.ae/en/category/sport/feed"},
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}


class WAMRSSScraper:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._seen_urls: set[str] = set()
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            headers=_HEADERS,
            timeout=30.0,
            follow_redirects=True,
        )
        logger.info("WAM HTTP scraper started")

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
        logger.info("WAM HTTP scraper stopped")

    async def __aenter__(self) -> "WAMRSSScraper":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    async def seed_seen_urls(self, known_urls: set[str]) -> None:
        self._seen_urls.update(known_urls)
        logger.info(f"Seeded {len(known_urls)} known article URLs into seen set")

    async def poll_subcategory(self, subcategory: dict) -> list[RawArticle]:
        name = subcategory["name"]
        url = subcategory["url"]

        logger.info(f"Polling WAM: {name}", url=url)

        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except Exception as exc:
            logger.error(f"Failed to fetch {url}: {exc}")
            return []

        articles = self._parse_html(resp.text, name, url)
        new_articles = [a for a in articles if a.url not in self._seen_urls]
        logger.info(f"{len(new_articles)} new articles in {name}")

        for a in new_articles:
            self._seen_urls.add(a.url)

        return new_articles

    def _parse_html(self, html: str, subcategory: str, base_url: str) -> list[RawArticle]:
        soup = BeautifulSoup(html, "lxml")
        articles = []

        # Strategy 1: Extract from JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    for item in data:
                        a = self._article_from_jsonld(item, subcategory)
                        if a:
                            articles.append(a)
                elif isinstance(data, dict):
                    a = self._article_from_jsonld(data, subcategory)
                    if a:
                        articles.append(a)
            except Exception:
                pass

        if articles:
            logger.debug(f"Found {len(articles)} articles via JSON-LD")
            return articles

        # Strategy 2: Parse <article> tags
        for tag in soup.find_all("article"):
            try:
                a_tag = tag.find("a", href=True)
                h = tag.find(["h1", "h2", "h3", "h4", "h5", "h6"])
                if not a_tag or not h:
                    continue
                url = _absolute_url(str(a_tag["href"]), WAM_BASE)
                title = h.get_text(strip=True)
                p = tag.find("p")
                summary = p.get_text(strip=True)[:500] if p else None
                img = tag.find("img")
                image_url = None
                if img:
                    src = img.get("src") or img.get("data-src") or ""
                    if src:
                        image_url = _absolute_url(str(src), WAM_BASE)
                articles.append(RawArticle(
                    title=title, url=url, image_url=image_url,
                    summary=summary, subcategory=subcategory,
                ))
            except Exception:
                pass

        if articles:
            logger.debug(f"Found {len(articles)} articles via <article> tags")
            return articles

        # Strategy 3: Extract from JSON-LD articleBody text
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                body = ""
                if isinstance(data, dict):
                    body = data.get("articleBody", "")
                if not body:
                    continue

                # Parse article chunks from articleBody
                # Format: "Title\n\nSummary text...\n\n"
                chunks = re.split(r'\n{2,}', body.strip())
                i = 0
                while i < len(chunks):
                    chunk = chunks[i].strip()
                    # Skip CSS/style blocks
                    if not chunk or '{' in chunk or '}' in chunk:
                        i += 1
                        continue
                    # This chunk is likely a title
                    title = chunk
                    summary = chunks[i+1].strip() if i+1 < len(chunks) else None
                    if summary and ('{' in summary or '}' in summary):
                        summary = None
                    if len(title) > 10 and len(title) < 300:
                        articles.append(RawArticle(
                            title=title,
                            url=base_url,  # no individual URL available here
                            summary=summary[:500] if summary else None,
                            subcategory=subcategory,
                        ))
                    i += 2
            except Exception:
                pass

        logger.debug(f"Found {len(articles)} articles via articleBody fallback")
        return articles

    def _article_from_jsonld(self, data: dict, subcategory: str) -> Optional[RawArticle]:
        if data.get("@type") not in ("NewsArticle", "Article", "BlogPosting"):
            return None
        url = data.get("url") or data.get("mainEntityOfPage", {}).get("@id", "")
        title = data.get("headline") or data.get("name", "")
        if not url or not title or url == "":
            return None
        if not url.startswith("http"):
            url = _absolute_url(url, WAM_BASE)
        summary = data.get("description", "")[:500] or None
        image = data.get("image")
        image_url = None
        if isinstance(image, str):
            image_url = image
        elif isinstance(image, dict):
            image_url = image.get("url")
        pub_date = None
        date_str = data.get("datePublished") or data.get("dateModified")
        if date_str:
            try:
                pub_date = datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
        return RawArticle(
            title=title, url=url, image_url=image_url,
            summary=summary, publish_date=pub_date, subcategory=subcategory,
        )


def _absolute_url(href: str, base: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)