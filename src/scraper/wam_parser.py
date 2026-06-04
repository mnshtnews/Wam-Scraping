"""
src/scraper/wam_parser.py
─────────────────────────
Selectors confirmed from diagnose5.py real HTML:

Card types (two layouts):
  style-2 (featured/top card):
    <div class="art-img single-blog-post style-2 ng-star-inserted">
      <div class="blog-thumbnail">
        <a href="/ar/article/..."><img src="https://assets.wam.ae/..."/></a>
      </div>
      <div class="blog-content">
        <a class="post-title description" href="/ar/article/...">TITLE TEXT</a>
        <div class="mt-1 description">
          <span class="text-muted font-weight-light"><small>SUMMARY</small></span>
        </div>
        <div><span class="post-date">منذ 9 ساعات</span></div>
      </div>
    </div>

  style-4 (list cards):
    <div class="single-blog-post d-flex style-4 art-img ng-star-inserted">
      <div class="blog-thumbnail position-relative">
        <a href="/ar/article/..."><img src="https://assets.wam.ae/..."/></a>
      </div>
      <div class="blog-content">
        <a class="post-title" href="/ar/article/...">TITLE TEXT</a>
        <div class="mt-1 description">
          <span class="text-muted font-weight-light"><small>SUMMARY</small></span>
        </div>
        <ul class="p-0"><li>
          <span class="post-date">منذ 11 ساعة  OR  الاثنين، 25 مايو 2026 3:53 م</span>
        </li></ul>
      </div>
    </div>

Key observations:
  - Title text is DIRECTLY inside <a class="post-title"> — no nested div
  - Image src is a full URL (https://assets.wam.ae/...) — not data-src
  - Date is relative Arabic ("منذ X ساعات") or absolute Arabic ("الاثنين، 25 مايو 2026")
  - URL format: /ar/article/<alphanum-id>-<url-encoded-arabic-slug>
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urljoin

import dateparser
from bs4 import BeautifulSoup, Tag
from loguru import logger

from src.core.models import RawArticle

WAM_BASE = "https://www.wam.ae"

# ── Card selector — both style-2 and style-4 share .single-blog-post ─────────
_CARD_SELECTOR = ".single-blog-post"

# ── Field selectors (confirmed from real HTML) ────────────────────────────────
_TITLE_SEL    = "a.post-title"          # works for both style-2 and style-4
_IMAGE_SEL    = ".blog-thumbnail img"   # img directly inside thumbnail anchor
_SUMMARY_SEL  = "span.text-muted small" # confirmed in both layouts
_DATE_SEL     = "span.post-date"        # confirmed in both layouts

_CONTENT_SELECTORS = [
    ".blog-content",
    ".article-body",
    ".article-content",
    "[class*='article-body']",
    "[class*='article-content']",
    ".ng-star-inserted",
]

_NOISE_SELECTORS = [
    "script", "style", "noscript", "iframe",
    ".related-articles", ".related-posts",
    "nav", "header", "footer",
    ".social-share", ".tags-section",
    ".comments-section", ".newsletter-signup",
    ".overlay",
]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_article_list(
    html: str,
    subcategory: str,
    base_url: str = WAM_BASE,
) -> list[RawArticle]:
    """Parse WAM category listing HTML → list of RawArticle stubs."""
    soup = BeautifulSoup(html, "lxml")
    seen_urls: set[str] = set()
    articles: list[RawArticle] = []

    cards = soup.select(_CARD_SELECTOR)
    if not cards:
        logger.warning(f"No cards found for subcategory={subcategory}")
        return []

    logger.debug(f"{len(cards)} cards found", subcategory=subcategory)

    for card in cards:
        try:
            article = _parse_card(card, subcategory, base_url)
            if article and article.url not in seen_urls:
                seen_urls.add(article.url)
                articles.append(article)
        except Exception as exc:
            logger.warning(f"Card parse error: {exc}")

    logger.info(f"Parsed {len(articles)} unique articles", subcategory=subcategory)
    return articles


def parse_article_detail(html: str, raw: RawArticle) -> RawArticle:
    """Enrich stub with full body, precise date, and image from detail page."""
    soup = BeautifulSoup(html, "lxml")
    updates: dict = {}

    if not raw.image_url:
        img = _extract_image_detail(soup)
        if img:
            updates["image_url"] = img

    if not raw.publish_date:
        dt = _extract_date_detail(soup)
        if dt:
            updates["publish_date"] = dt

    content = _extract_content(soup)
    if content:
        updates["content"] = content

    return raw.model_copy(update=updates) if updates else raw


# ─────────────────────────────────────────────────────────────────────────────
# Card parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_card(card: Tag, subcategory: str, base_url: str) -> Optional[RawArticle]:

    # ── Title ─────────────────────────────────────────────────────────────────
    title_tag = card.select_one(_TITLE_SEL)
    if not title_tag:
        return None

    title = _clean(title_tag.get_text(strip=True))
    if not title:
        return None

    # ── URL ───────────────────────────────────────────────────────────────────
    href = str(title_tag.get("href", "")).strip()
    if not href:
        return None

    url = _abs(href, base_url)
    if not _is_article_url(url):
        logger.debug(f"Rejected URL: {url}")
        return None

    # ── Image ─────────────────────────────────────────────────────────────────
    image_url: Optional[str] = None
    img_tag = card.select_one(_IMAGE_SEL)
    if img_tag:
        # src is already a full https://assets.wam.ae/... URL
        src = str(img_tag.get("src") or img_tag.get("data-src") or "")
        if src and "placeholder" not in src:
            image_url = src if src.startswith("http") else _abs(src, base_url)

    # ── Summary ───────────────────────────────────────────────────────────────
    summary: Optional[str] = None
    summary_tag = card.select_one(_SUMMARY_SEL)
    if summary_tag:
        text = _clean(summary_tag.get_text(strip=True))
        if len(text) > 15:
            summary = text[:500]

    # ── Date ──────────────────────────────────────────────────────────────────
    publish_date: Optional[datetime] = None
    date_tag = card.select_one(_DATE_SEL)
    if date_tag:
        # Strip the clock icon text — get only the date text
        raw_text = date_tag.get_text(separator=" ", strip=True)
        # Remove icon artifacts (fa icon renders as empty or special chars)
        date_text = _clean(re.sub(r'[\uf000-\uf8ff]', '', raw_text))
        publish_date = _parse_arabic_date(date_text)

    return RawArticle(
        title=title,
        url=url,
        image_url=image_url,
        summary=summary,
        publish_date=publish_date,
        subcategory=subcategory,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detail page extractors
# ─────────────────────────────────────────────────────────────────────────────

def _extract_content(soup: BeautifulSoup) -> str:
    content_div: Optional[Tag] = None
    for sel in _CONTENT_SELECTORS:
        content_div = soup.select_one(sel)
        if content_div:
            break

    if not content_div:
        # fallback: div with most paragraph text
        best, best_score = None, 0
        for div in soup.find_all(["div", "section"]):
            score = sum(len(p.get_text()) for p in div.find_all("p"))
            if score > best_score:
                best_score, best = score, div
        content_div = best if best_score > 100 else None

    if not content_div:
        return ""

    for sel in _NOISE_SELECTORS:
        for el in content_div.select(sel):
            el.decompose()

    paras = []
    for p in content_div.find_all("p"):
        text = _clean(p.get_text(separator=" "))
        if len(text) >= 20:
            paras.append(text)

    return "\n\n".join(paras)


def _extract_image_detail(soup: BeautifulSoup) -> Optional[str]:
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

    img = soup.select_one(".blog-thumbnail img, article img, figure img")
    if img:
        src = str(img.get("src") or img.get("data-src") or "")
        if src and "placeholder" not in src:
            return src if src.startswith("http") else _abs(src, WAM_BASE)

    return None


def _extract_date_detail(soup: BeautifulSoup) -> Optional[datetime]:
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

    date_tag = soup.select_one("span.post-date")
    if date_tag:
        text = _clean(date_tag.get_text(separator=" "))
        return _parse_arabic_date(text)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Date parsing — Arabic relative + absolute
# ─────────────────────────────────────────────────────────────────────────────

# Arabic number map
_AR_NUMS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Relative date patterns (Arabic)
_RELATIVE_PATTERNS = [
    (r"منذ\s+(\d+)\s+ثانية",  lambda n: timedelta(seconds=n)),
    (r"منذ\s+(\d+)\s+دقيقة",  lambda n: timedelta(minutes=n)),
    (r"منذ\s+(\d+)\s+ساعات?", lambda n: timedelta(hours=n)),
    (r"منذ\s+(\d+)\s+أيام?",  lambda n: timedelta(days=n)),
    (r"منذ\s+(\d+)\s+أسابيع?",lambda n: timedelta(weeks=n)),
    (r"منذ\s+يومين",           lambda n: timedelta(days=2)),
    (r"منذ\s+ساعتين",          lambda n: timedelta(hours=2)),
    (r"منذ\s+دقيقتين",         lambda n: timedelta(minutes=2)),
    (r"أمس",                   lambda n: timedelta(days=1)),
]

def _parse_arabic_date(text: str) -> Optional[datetime]:
    if not text or len(text) < 3:
        return None

    # Normalise Arabic-Indic numerals
    text = text.translate(_AR_NUMS).strip()
    now = datetime.now(timezone.utc)

    # Try relative patterns
    for pattern, delta_fn in _RELATIVE_PATTERNS:
        m = re.search(pattern, text)
        if m:
            try:
                n = int(m.group(1)) if m.lastindex else 0
                return now - delta_fn(n)
            except Exception:
                return now - delta_fn(0)

    # Try ISO format
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        pass

    # Try dateparser with Arabic locale
    try:
        parsed = dateparser.parse(
            text,
            settings={
                "PREFER_DAY_OF_MONTH": "first",
                "TIMEZONE": "Asia/Dubai",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "LANGUAGES": ["ar"],
            },
        )
        return parsed
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _is_article_url(url: str) -> bool:
    """
    Accept:  https://www.wam.ae/ar/article/<id>-<slug>
    Reject:  category pages, homepage, other domains
    """
    if not url.startswith("https://www.wam.ae"):
        return False
    if "/article/" not in url:
        return False
    after = url.split("/article/")[-1].strip("/")
    return len(after) > 5


def _clean(text: str) -> str:
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u00a0]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _abs(href: str, base: str) -> str:
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base, href)
