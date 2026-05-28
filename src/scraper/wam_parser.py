"""
src/scraper/wam_parser.py
─────────────────────────
HTML parsing layer for WAM (wam.ae) articles.

Modelled on FilGoal's filgoal_parser.py in quality and structure:
  - Centralised selectors
  - Multi-strategy content extraction with scored fallback
  - Noise removal before text extraction
  - JSON-LD as primary date/image source (most reliable)
  - Open Graph as secondary source
  - Visible DOM as last resort

WAM HTML structure (discovered from real pages):
  Angular renders articles inside:
    app-article-item-bottom-text   ← listing card (primary selector)
      .art-img.single-blog-post    ← card container
        a[href]                    ← article URL
        .post-title                ← article title
        img / .blog-thumbnail      ← thumbnail
        .post-date / time          ← date
        small / .description       ← excerpt

  Article detail page:
    .article-body / .article-content  ← full body text
    .ng-star-inserted p               ← Angular-rendered paragraphs
    script[type="application/ld+json"] ← JSON-LD metadata
    meta[property="og:image"]          ← hero image
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

# ── Selectors — centralised so changes only require edits here ────────────────

_ARTICLE_CARD_SELECTORS = [
    "app-article-item-bottom-text",     # primary Angular component
    ".art-img.single-blog-post",        # rendered card container
    ".single-blog-post",
    "article.blog-post",
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
    ".ng-star-inserted",
]

_IMAGE_SELECTORS = [
    ".blog-thumbnail img",
    ".article-image img",
    "article img",
    "figure img",
    "img[class*='article']",
    "img[class*='hero']",
    "img[class*='thumb']",
]

_DATE_SELECTORS = [
    ".post-date",
    "time",
    "[class*='date']",
    "[class*='time']",
    "span.text-muted",
    "[class*='publish']",
]

# Noise inside article body to strip before text extraction
_NOISE_SELECTORS = [
    "script", "style", "noscript", "iframe",
    "ins", ".adsbygoogle",
    ".related-articles", ".related-posts", "[class*='related']",
    "nav", "header", "footer",
    ".social-share", "[class*='share']",
    ".tags-section", ".article-tags",
    ".match-widget", ".team-widget", "[class*='widget']",
    ".comments-section", ".newsletter-signup",
    ".author-bio", ".author-box",
]

WAM_BASE = "https://www.wam.ae"


# ─────────────────────────────────────────────────────────────────────────────
# Public API — mirrors filgoal_parser.py interface exactly
# ─────────────────────────────────────────────────────────────────────────────

def parse_article_list(
    html: str,
    subcategory: str,
    base_url: str = WAM_BASE,
) -> list[RawArticle]:
    """
    Parse a WAM subcategory listing page and return RawArticle stubs.
    Stubs contain title, URL, image, summary, and date.
    Full article body is fetched separately in parse_article_detail().
    """
    soup = BeautifulSoup(html, "lxml")
    articles: list[RawArticle] = []

    # Find article card containers
    cards: list[Tag] = []
    for selector in _ARTICLE_CARD_SELECTORS:
        found = soup.select(selector)
        if found:
            cards = found
            logger.debug(
                f"Found {len(found)} cards with selector '{selector}'",
                subcategory=subcategory,
            )
            break

    if not cards:
        logger.warning(
            f"No article cards found in WAM listing HTML for subcategory={subcategory}. "
            f"Angular may not have rendered yet."
        )
        return []

    for card in cards:
        try:
            article = _parse_card(card, subcategory, base_url)
            if article:
                articles.append(article)
        except Exception as exc:
            logger.warning(f"Failed to parse WAM article card: {exc}")

    logger.debug(f"Parsed {len(articles)} articles from WAM listing", subcategory=subcategory)
    return articles


def parse_article_detail(html: str, raw: RawArticle) -> RawArticle:
    """
    Parse a full WAM article detail page.
    Enriches the RawArticle stub with full body text, precise date, and image.
    Mirrors filgoal_parser.parse_article_detail() in structure and quality.
    """
    soup = BeautifulSoup(html, "lxml")
    updates: dict = {}

    # ── Image — JSON-LD first, then OG, then DOM ──────────────────────────────
    if not raw.image_url:
        image_url = _extract_image(soup)
        if image_url:
            updates["image_url"] = image_url

    # ── Date — JSON-LD first, then OG, then DOM ───────────────────────────────
    if not raw.publish_date:
        publish_date = _extract_date(soup)
        if publish_date:
            updates["publish_date"] = publish_date

    # ── Full content — scored extraction with noise removal ───────────────────
    content = _extract_full_content(soup)
    if content:
        updates["content"] = content

    if updates:
        raw = raw.model_copy(update=updates)

    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Content extraction — modelled on FilGoal's _extract_full_content()
# ─────────────────────────────────────────────────────────────────────────────

def _extract_full_content(soup: BeautifulSoup) -> str:
    """
    Extract clean article body text from a WAM detail page.

    Strategy (in order):
      1. Try known WAM article body selectors
      2. Fall back to the div with the most paragraph text (scored)
      3. Strip all noise, then collect <p> tags
    """
    # Try known selectors first
    content_div: Optional[Tag] = None
    for selector in _CONTENT_SELECTORS:
        content_div = soup.select_one(selector)
        if content_div:
            logger.debug(f"WAM content found via selector '{selector}'")
            break

    # Scored fallback — find the div with the most meaningful paragraph text
    if not content_div:
        content_div = _find_content_rich_div(soup)

    if not content_div:
        return ""

    # Remove noise in-place
    for selector in _NOISE_SELECTORS:
        for el in content_div.select(selector):
            el.decompose()

    # Extract and clean all <p> tags
    paragraphs: list[str] = []
    for p in content_div.find_all("p"):
        # Skip captions and short UI fragments
        cls = " ".join(p.get("class") or [])
        if "caption" in cls or "credit" in cls:
            continue

        text = _clean_text(p.get_text(separator=" ", strip=True))
        if len(text) >= 20:
            paragraphs.append(text)

    return "\n\n".join(paragraphs)


def _find_content_rich_div(soup: BeautifulSoup) -> Optional[Tag]:
    """
    Find the div/section containing the most substantive paragraph text.
    Identical logic to FilGoalParser._find_content_rich_div().
    """
    best: Optional[Tag] = None
    best_score = 0

    for div in soup.find_all(["div", "section", "main"]):
        paragraphs = div.find_all("p", recursive=False)
        if not paragraphs:
            paragraphs = div.find_all("p")

        text_len = sum(len(p.get_text(strip=True)) for p in paragraphs)
        if text_len > best_score:
            best_score = text_len
            best = div

    return best if best_score > 100 else None


# ─────────────────────────────────────────────────────────────────────────────
# Field extractors
# ─────────────────────────────────────────────────────────────────────────────

def _extract_image(soup: BeautifulSoup) -> Optional[str]:
    """
    Extract article hero image.
    Priority: Open Graph > JSON-LD > DOM selectors.
    """
    # 1. Open Graph (most reliable — set by server)
    og = soup.find("meta", property="og:image")
    if og:
        content = og.get("content")
        if content:
            return str(content)

    # 2. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                img = data.get("image")
                if isinstance(img, str) and img.startswith("http"):
                    return img
                if isinstance(img, dict):
                    url = img.get("url")
                    if url:
                        return str(url)
        except Exception:
            continue

    # 3. DOM selectors
    for selector in _IMAGE_SELECTORS:
        img_tag = soup.select_one(selector)
        if img_tag:
            src = (
                img_tag.get("data-src")
                or img_tag.get("src")
                or ""
            )
            if src and "placeholder" not in str(src):
                return _absolute_url(str(src), WAM_BASE)

    return None


def _extract_date(soup: BeautifulSoup) -> Optional[datetime]:
    """
    Extract article publish date.
    Priority: JSON-LD > Open Graph > <time> element > visible text.
    Mirrors filgoal_parser._extract_date() in quality.
    """
    # 1. JSON-LD (most reliable)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                date_str = data.get("datePublished") or data.get("dateModified")
                if date_str:
                    return datetime.fromisoformat(
                        str(date_str).replace("Z", "+00:00")
                    )
        except Exception:
            continue

    # 2. Open Graph
    og = soup.find("meta", property="article:published_time")
    if og:
        content = og.get("content")
        if content:
            try:
                return datetime.fromisoformat(
                    str(content).replace("Z", "+00:00")
                )
            except Exception:
                pass

    # 3. <time> element
    time_el = soup.find("time")
    if time_el:
        dt = time_el.get("datetime") or time_el.get_text(strip=True)
        try:
            return datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
        except Exception:
            pass

    # 4. Visible date text via known selectors
    for sel in _DATE_SELECTORS:
        date_tag = soup.select_one(sel)
        if date_tag:
            text = date_tag.get("datetime") or date_tag.get_text(strip=True)
            if text:
                parsed = _parse_date(str(text))
                if parsed:
                    return parsed

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Listing card parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_card(
    card: Tag,
    subcategory: str,
    base_url: str,
) -> Optional[RawArticle]:
    """Extract structured data from a single WAM article card element."""

    # ── Title & URL ───────────────────────────────────────────────────────────
    title_tag: Optional[Tag] = None
    for sel in _TITLE_SELECTORS:
        title_tag = card.select_one(sel)
        if title_tag:
            break

    if not title_tag:
        return None

    title = _clean_text(title_tag.get_text(strip=True))

    # Extract href — from title tag if it's an <a>, else nearest anchor
    href: Optional[str] = None
    if title_tag.name == "a":
        href = str(title_tag.get("href", ""))
    if not href:
        anchor = card.find("a", href=True)
        if anchor:
            href = str(anchor.get("href", ""))

    if not href or not title:
        return None

    url = _absolute_url(href, base_url)

    # Reject non-article URLs (navigation links etc.)
    if not _looks_like_article_url(url):
        return None

    # ── Image ─────────────────────────────────────────────────────────────────
    image_url: Optional[str] = None
    for sel in _IMAGE_SELECTORS:
        img = card.select_one(sel)
        if img:
            src = (
                str(img.get("data-src", ""))
                or str(img.get("src", ""))
            )
            if src and "placeholder" not in src:
                image_url = _absolute_url(src, base_url)
            break

    # ── Summary / excerpt ──────────────────────────────────────────────────────
    summary: Optional[str] = None
    for sel in ["small", ".description", "p"]:
        summary_tag = card.select_one(sel)
        if summary_tag:
            text = _clean_text(summary_tag.get_text(strip=True))
            if len(text) > 10:
                summary = text[:500]
                break

    # ── Date ──────────────────────────────────────────────────────────────────
    publish_date: Optional[datetime] = None
    for sel in _DATE_SELECTORS:
        date_tag = card.select_one(sel)
        if date_tag:
            dt_attr = date_tag.get("datetime")
            text = str(dt_attr) if dt_attr else date_tag.get_text(strip=True)
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
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse a date string — handles ISO, relative ('2 hours ago'), Arabic."""
    if not date_str or len(date_str) < 4:
        return None
    try:
        # Try ISO first (fast path)
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
    """Normalize whitespace and strip zero-width characters."""
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _absolute_url(href: str, base: str) -> str:
    """Convert a relative URL to absolute."""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)


def _looks_like_article_url(url: str) -> bool:
    """
    Reject navigation / category URLs that are not individual articles.
    WAM article URLs contain a numeric segment e.g. /en/article/12345/...
    """
    if not url.startswith("https://www.wam.ae"):
        return False
    # Must have some path beyond just the domain
    path = url.replace("https://www.wam.ae", "")
    if len(path) < 5:
        return False
    # Reject pure category URLs like /en/sports/football
    skip_patterns = ["/en/sports$", "/en/category/", "/en/$", "^https://www.wam.ae/?$"]
    for pattern in skip_patterns:
        if re.search(pattern, url):
            return False
    return True
