"""
src/scraper/parser.py
─────────────────────
HTML parsing layer — converts raw Playwright page content into
structured RawArticle objects.

WAM uses Angular with server-side rendering, so most content is
available in the DOM after networkidle, but some elements render
asynchronously. This module handles both cases.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import dateparser
from bs4 import BeautifulSoup, Tag
from loguru import logger

from src.core.models import RawArticle

# ── Selectors — centralised here so changes only require edits in one place ──
_ARTICLE_LIST_SELECTORS = [
    "app-article-item-bottom-text",          # primary Angular component
    ".art-img.single-blog-post",             # rendered card container
    "article",
    "[class*='article-item']",
    "[class*='blog-post']",
]

_TITLE_SELECTORS = [
    "a.post-title",
    ".post-title",
    "h1.article-title",
    "h2.article-title",
    "[class*='post-title']",
    "h1", "h2", "h3",
]

_CONTENT_SELECTORS = [
    ".article-body",
    ".article-content",
    "[class*='article-body']",
    "[class*='article-content']",
    ".content-area",
    "main article",
    ".ng-star-inserted p",
]

_IMAGE_SELECTORS = [
    ".blog-thumbnail img",
    ".article-image img",
    "article img",
    "figure img",
    "img[class*='article']",
    "img[class*='hero']",
]

_DATE_SELECTORS = [
    ".post-date",
    "time",
    "[class*='date']",
    "[class*='time']",
    "span.text-muted",
]

WAM_BASE = "https://www.wam.ae"


def parse_article_list(html: str, subcategory: str, base_url: str = WAM_BASE) -> list[RawArticle]:
    """
    Parse a subcategory listing page and return a list of RawArticle stubs.
    These stubs contain the title, URL, image, summary, and date,
    but not the full article body (fetched separately in article detail).
    """
    soup = BeautifulSoup(html, "lxml")
    articles: list[RawArticle] = []

    # Find all article card containers
    cards: list[Tag] = []
    for selector in _ARTICLE_LIST_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            break

    if not cards:
        logger.warning(f"No article cards found in listing HTML for subcategory={subcategory}")
        return []

    for card in cards:
        try:
            article = _parse_card(card, subcategory, base_url)
            if article:
                articles.append(article)
        except Exception as exc:
            logger.warning(f"Failed to parse article card: {exc}")

    logger.debug(f"Parsed {len(articles)} articles from listing", subcategory=subcategory)
    return articles


def parse_article_detail(html: str, raw: RawArticle) -> RawArticle:
    """
    Parse a full article detail page and enrich the RawArticle with
    the complete body text and a more precise publish date.
    """
    soup = BeautifulSoup(html, "lxml")

    # ── Full content ──────────────────────────────────────────────────────────
    content_tag: Optional[Tag] = None
    for selector in _CONTENT_SELECTORS:
        content_tag = soup.select_one(selector)
        if content_tag:
            break

    if content_tag:
        # Remove script/style tags from body
        for tag in content_tag.find_all(["script", "style", "noscript"]):
            tag.decompose()
        content = content_tag.get_text(separator="\n", strip=True)
        raw = raw.model_copy(update={"content": content})

    # ── Precise publish date ──────────────────────────────────────────────────
    date_str = _extract_date_string(soup)
    if date_str:
        parsed_date = _parse_date(date_str)
        if parsed_date:
            raw = raw.model_copy(update={"publish_date": parsed_date})

    # ── High-res image ────────────────────────────────────────────────────────
    if not raw.image_url:
        img_tag = None
        for selector in _IMAGE_SELECTORS:
            img_tag = soup.select_one(selector)
            if img_tag:
                break
        if img_tag:
            src = img_tag.get("src") or img_tag.get("data-src") or ""
            if src:
                raw = raw.model_copy(update={"image_url": _absolute_url(str(src), WAM_BASE)})

    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_card(card: Tag, subcategory: str, base_url: str) -> Optional[RawArticle]:
    """Extract structured data from a single article card element."""

    # ── Title & URL ───────────────────────────────────────────────────────────
    title_tag: Optional[Tag] = None
    for sel in _TITLE_SELECTORS:
        title_tag = card.select_one(sel)
        if title_tag:
            break

    if not title_tag:
        return None

    title = title_tag.get_text(strip=True)
    href = title_tag.get("href") if title_tag.name == "a" else None

    # If title tag is not an anchor, look for the nearest anchor
    if not href:
        anchor = card.find("a", href=True)
        href = anchor.get("href") if anchor else None

    if not href or not title:
        return None

    url = _absolute_url(str(href), base_url)

    # ── Image ──────────────────────────────────────────────────────────────────
    image_url: Optional[str] = None
    for sel in _IMAGE_SELECTORS:
        img = card.select_one(sel)
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src:
                image_url = _absolute_url(str(src), base_url)
            break

    # ── Summary / excerpt ─────────────────────────────────────────────────────
    summary: Optional[str] = None
    summary_tag = card.select_one("small") or card.select_one(".description")
    if summary_tag:
        summary = summary_tag.get_text(strip=True)[:500]

    # ── Date ──────────────────────────────────────────────────────────────────
    publish_date: Optional[datetime] = None
    date_str = _extract_date_string(card)
    if date_str:
        publish_date = _parse_date(date_str)

    return RawArticle(
        title=title,
        url=url,
        image_url=image_url,
        summary=summary,
        publish_date=publish_date,
        subcategory=subcategory,
    )


def _extract_date_string(tag: Tag) -> Optional[str]:
    """Find and return the first recognisable date string in a tag."""
    for sel in _DATE_SELECTORS:
        date_tag = tag.select_one(sel)
        if date_tag:
            # Check datetime attribute first (most reliable)
            dt_attr = date_tag.get("datetime")
            if dt_attr:
                return str(dt_attr)
            text = date_tag.get_text(strip=True)
            if text:
                return text
    return None


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse a date string using dateparser (handles relative times like '20 hours ago')."""
    if not date_str:
        return None
    try:
        parsed = dateparser.parse(
            date_str,
            settings={
                "PREFER_DAY_OF_MONTH": "first",
                "TIMEZONE": "Asia/Dubai",
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )
        return parsed
    except Exception:
        return None


def _absolute_url(href: str, base: str) -> str:
    """Convert a relative URL to absolute."""
    if href.startswith("http"):
        return href
    return urljoin(base, href)
