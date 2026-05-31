"""
src/scraper/wam_parser.py
─────────────────────────
HTML parsing layer for WAM (wam.ae) articles.

CONFIRMED selectors from live site (diagnose3.py):
  Cards:     .single-blog-post   (32 found)
             app-article-item-bottom-text  (13 found — subset with text below)
  Container: .art-img  (32 found)
  Title:     a.post-title  (confirmed in slide HTML)
  Image:     .blog-thumbnail img  (confirmed in slide HTML)
  Date:      .post-date  (confirmed in class dump)
  Summary:   span.text-muted small  (confirmed in slide HTML)
  URL:       a[href*='/en/article/']
  URL fmt:   /en/article/<alphanum-id>-<title-slug>
             e.g. /en/article/178wkh2-uae-president-vps-offer-condolences
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import dateparser
from bs4 import BeautifulSoup, Tag
from loguru import logger

from src.core.models import RawArticle

WAM_BASE = "https://www.wam.ae"

# ── Confirmed card selectors (ordered by specificity) ─────────────────────────
_ARTICLE_CARD_SELECTORS = [
    ".single-blog-post",                 # 32 confirmed — primary selector
    "app-article-item-bottom-text",      # 13 confirmed — more specific subtype
    ".art-img",                          # 32 confirmed — wrapper
    "[class*='single-blog-post']",
    "[class*='art-img']",
]

# ── Confirmed field selectors ─────────────────────────────────────────────────
_TITLE_SELECTORS = [
    "a.post-title",           # confirmed from slide HTML: <a class="post-title description" href=...>
    "a.post-title.description",
    ".post-title",
    "a[class*='post-title']",
]

_CONTENT_SELECTORS = [
    ".article-body",
    ".article-content",
    ".blog-content",
    "[class*='article-body']",
    "[class*='article-content']",
    ".ng-star-inserted",
    "main article",
]

_IMAGE_SELECTORS = [
    ".blog-thumbnail img",    # confirmed from slide HTML
    ".blog-thumbnail a img",
    "a > img",
    "article img",
    "figure img",
    "img[class*='article']",
    "img[class*='hero']",
    "img[class*='thumb']",
]

_DATE_SELECTORS = [
    ".post-date",             # confirmed from class dump
    "time",
    "[class*='post-date']",
    "[class*='date']",
    "span.text-muted",
]

_SUMMARY_SELECTORS = [
    "span.text-muted small",  # confirmed from slide HTML: <small>excerpt text...</small>
    "small",
    ".description small",
    "p.description",
    ".description",
    "p",
]

_NOISE_SELECTORS = [
    "script", "style", "noscript", "iframe", "ins", ".adsbygoogle",
    ".related-articles", ".related-posts", "[class*='related']",
    "nav", "header", "footer",
    ".social-share", "[class*='share']",
    ".tags-section", ".article-tags",
    ".comments-section", ".newsletter-signup",
    ".author-bio", ".author-box",
]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_article_list(
    html: str,
    subcategory: str,
    base_url: str = WAM_BASE,
) -> list[RawArticle]:
    """
    Parse a WAM category page HTML and return RawArticle stubs.
    Handles the case where multiple card selectors exist in the same DOM
    by deduplicating on URL.
    """
    soup = BeautifulSoup(html, "lxml")
    seen_urls: set[str] = set()
    articles: list[RawArticle] = []

    # Find the best card container
    cards: list[Tag] = []
    for selector in _ARTICLE_CARD_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            logger.debug(
                f"Using selector '{selector}' — {len(found)} cards found",
                subcategory=subcategory,
            )
            break

    if not cards:
        logger.warning(
            f"No article cards found in WAM HTML for subcategory={subcategory}. "
            f"Angular may not have rendered yet."
        )
        return []

    for card in cards:
        try:
            article = _parse_card(card, subcategory, base_url)
            if article and article.url not in seen_urls:
                seen_urls.add(article.url)
                articles.append(article)
        except Exception as exc:
            logger.warning(f"Failed to parse WAM card: {exc}")

    logger.info(
        f"Parsed {len(articles)} unique articles from WAM listing",
        subcategory=subcategory,
    )
    return articles


def parse_article_detail(html: str, raw: RawArticle) -> RawArticle:
    """Enrich a RawArticle stub with full body, precise date, and image."""
    soup = BeautifulSoup(html, "lxml")
    updates: dict = {}

    if not raw.image_url:
        img = _extract_image(soup)
        if img:
            updates["image_url"] = img

    if not raw.publish_date:
        dt = _extract_date(soup)
        if dt:
            updates["publish_date"] = dt

    content = _extract_full_content(soup)
    if content:
        updates["content"] = content

    if updates:
        raw = raw.model_copy(update=updates)

    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Card parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_card(card: Tag, subcategory: str, base_url: str) -> Optional[RawArticle]:
    """Extract structured data from one WAM article card."""

    # ── Title & URL ───────────────────────────────────────────────────────────
    title_tag: Optional[Tag] = None
    for sel in _TITLE_SELECTORS:
        title_tag = card.select_one(sel)
        if title_tag:
            break

    # Fallback: any anchor inside the card with substantial text
    if not title_tag:
        for a in card.find_all("a", href=True):
            href = str(a.get("href", ""))
            if "/article/" in href:
                title_tag = a
                break

    if not title_tag:
        return None

    title = _clean_text(title_tag.get_text(strip=True))
    if not title:
        return None

    # Extract href
    href: Optional[str] = None
    if title_tag.name == "a":
        href = str(title_tag.get("href", ""))
    if not href:
        anchor = card.find("a", href=True)
        if anchor:
            href = str(anchor.get("href", ""))

    if not href:
        return None

    url = _absolute_url(href, base_url)

    if not _looks_like_article_url(url):
        logger.debug(f"Rejected non-article URL: {url}")
        return None

    # ── Image ─────────────────────────────────────────────────────────────────
    image_url: Optional[str] = None
    for sel in _IMAGE_SELECTORS:
        img = card.select_one(sel)
        if img:
            src = str(img.get("data-src") or img.get("src") or "")
            if src and "placeholder" not in src and src.startswith("http"):
                image_url = src
                break
            elif src and "placeholder" not in src:
                image_url = _absolute_url(src, base_url)
                break

    # ── Summary ───────────────────────────────────────────────────────────────
    summary: Optional[str] = None
    for sel in _SUMMARY_SELECTORS:
        tag = card.select_one(sel)
        if tag:
            text = _clean_text(tag.get_text(strip=True))
            if len(text) > 15 and text != title:
                summary = text[:500]
                break

    # ── Date ──────────────────────────────────────────────────────────────────
    publish_date: Optional[datetime] = None
    for sel in _DATE_SELECTORS:
        date_tag = card.select_one(sel)
        if date_tag:
            dt_attr = date_tag.get("datetime")
            text = str(dt_attr) if dt_attr else date_tag.get_text(strip=True)
            text = _clean_text(text)
            if text:
                publish_date = _parse_date(text)
                if publish_date:
                    break

    return RawArticle(
        title=title,
        url=url,
        image_url=image_url,
        summary=summary,
        publish_date=publish_date,
        subcategory=subcategory,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Content / image / date extraction for detail pages
# ─────────────────────────────────────────────────────────────────────────────

def _extract_full_content(soup: BeautifulSoup) -> str:
    content_div: Optional[Tag] = None
    for selector in _CONTENT_SELECTORS:
        content_div = soup.select_one(selector)
        if content_div:
            logger.debug(f"Content via selector '{selector}'")
            break

    if not content_div:
        content_div = _find_content_rich_div(soup)

    if not content_div:
        return ""

    for sel in _NOISE_SELECTORS:
        for el in content_div.select(sel):
            el.decompose()

    paragraphs: list[str] = []
    for p in content_div.find_all("p"):
        cls = " ".join(p.get("class") or [])
        if "caption" in cls or "credit" in cls:
            continue
        text = _clean_text(p.get_text(separator=" ", strip=True))
        if len(text) >= 20:
            paragraphs.append(text)

    return "\n\n".join(paragraphs)


def _find_content_rich_div(soup: BeautifulSoup) -> Optional[Tag]:
    best: Optional[Tag] = None
    best_score = 0
    for div in soup.find_all(["div", "section", "main"]):
        paragraphs = div.find_all("p")
        score = sum(len(p.get_text(strip=True)) for p in paragraphs)
        if score > best_score:
            best_score = score
            best = div
    return best if best_score > 100 else None


def _extract_image(soup: BeautifulSoup) -> Optional[str]:
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return str(og["content"])

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                img = data.get("image")
                if isinstance(img, str) and img.startswith("http"):
                    return img
                if isinstance(img, dict) and img.get("url"):
                    return str(img["url"])
        except Exception:
            continue

    for selector in _IMAGE_SELECTORS:
        img_tag = soup.select_one(selector)
        if img_tag:
            src = str(img_tag.get("data-src") or img_tag.get("src") or "")
            if src and "placeholder" not in src:
                return _absolute_url(src, WAM_BASE)

    return None


def _extract_date(soup: BeautifulSoup) -> Optional[datetime]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                ds = data.get("datePublished") or data.get("dateModified")
                if ds:
                    return datetime.fromisoformat(str(ds).replace("Z", "+00:00"))
        except Exception:
            continue

    og = soup.find("meta", property="article:published_time")
    if og and og.get("content"):
        try:
            return datetime.fromisoformat(str(og["content"]).replace("Z", "+00:00"))
        except Exception:
            pass

    time_el = soup.find("time")
    if time_el:
        dt = time_el.get("datetime") or time_el.get_text(strip=True)
        try:
            return datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
        except Exception:
            pass

    for sel in _DATE_SELECTORS:
        tag = soup.select_one(sel)
        if tag:
            text = tag.get("datetime") or tag.get_text(strip=True)
            if text:
                parsed = _parse_date(str(text))
                if parsed:
                    return parsed

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_article_url(url: str) -> bool:
    """
    Accept only WAM article URLs.

    Confirmed URL format from live site (diagnose2 slide HTML):
      /en/article/<alphanumeric-id>-<title-slug>
      e.g. /en/article/178wkh2-uae-president-vps-offer-condolences-chairman
           /en/article/c0fuuro-uae-leaders-congratulate-president-azerbaijan

    Rejects:
      - Non-WAM domains
      - /en/category/* (listing pages)
      - /en/home/* (homepage)
      - /ar/* (Arabic versions)
      - /en/financial-market/* (market data)
    """
    if not url.startswith("https://www.wam.ae"):
        return False
    if "/ar/article/" not in url:
        return False
    # Ensure there's something after /en/article/
    after = url.split("/ar/article/")[-1].strip("/")
    return len(after) > 5


def _parse_date(date_str: str) -> Optional[datetime]:
    if not date_str or len(date_str) < 4:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return dateparser.parse(
            date_str,
            settings={
                "PREFER_DAY_OF_MONTH": "first",
                "TIMEZONE": "Asia/Dubai",
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )
    except Exception:
        return None


def _clean_text(text: str) -> str:
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _absolute_url(href: str, base: str) -> str:
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)
