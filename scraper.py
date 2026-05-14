# === Selector map ===
# Entertainment listing: https://ekantipur.com/entertainment
#   Loop unit: div.category-inner-wrapper
#   Title:     .category-description h2 a (text, href)
#   Author:    .author-name a (one or more, join ", ")
#   Image:     .category-image img -> read data-src (lazy)
#   Page category label: .category-name a (in <header>)
#
# Article detail page:
#   JSON-LD: script[type="application/ld+json"] -> headline, image[0], author.name, datePublished
#
# Cartoon page: https://ekantipur.com/cartoon
#   Loop unit (first = today): div.cartoon-wrapper
#   Image data-src: .cartoon-image figure a img
#   Full-res image (direct): .cartoon-image figure a -> href
#   Title:    .cartoon-description p (strip trailing " - ")
#   Author:   NOT in DOM, EXIF, or XMP. Return None.
#
# CDN unwrap: thumb URLs look like
#   https://assets-cdn-api.ekantipur.com/thumb.php?src=<actual>&w=701&h=0
#   Parse query param "src" to recover original asset URL.

# Scrapes ekantipur entertainment + cartoon pages; writes one validated JSON blob.

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import argparse
from argparse import BooleanOptionalAction

from playwright.async_api import BrowserContext, Page, async_playwright
from pydantic import BaseModel, Field

# Mimic a normal desktop Chrome session (fewer empty responses than default Playwright UA).
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

ENTERTAINMENT_URL = "https://ekantipur.com/entertainment"
OUTPUT_PATH = "output.json"
SCRAPER_VERSION = "1.0.0"


# Output models: matches the fixed consumer schema (no extra keys in JSON).
class Article(BaseModel):
    title: str
    image_url: str
    category: str
    author: str | None


class Cartoon(BaseModel):
    title: str
    image_url: str
    author: str | None


class ScrapeResult(BaseModel):
    entertainment_news: list[Article]
    cartoon_of_the_day: Cartoon
    meta: dict = Field(alias="_meta")


# Timestamps in log lines; level is raised to DEBUG when --verbose is passed.
def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


# Resolve CDN thumbnail URLs to the underlying image URL when applicable.
def unwrap_cdn_url(url: str) -> str:
    if not url.startswith("https://assets-cdn-api.ekantipur.com/thumb.php"):
        return url
    parsed = urlparse(url)
    src_vals = parse_qs(parsed.query).get("src")
    if not src_vals:
        return url
    return unquote(src_vals[0])


# Opens a throwaway tab per article; failures are soft (empty dict, debug log).
async def enrich_from_jsonld(context: BrowserContext, article_url: str) -> dict:
    log = logging.getLogger(__name__)

    detail = await context.new_page()
    try:
        await detail.goto(article_url, wait_until="domcontentloaded")
        raw = await detail.locator("script[type='application/ld+json']").first.inner_text()
        data = json.loads(raw)
        if data.get("@type") != "NewsArticle":
            return {}
        return {
            "canonical_author": data.get("author", {}).get("name"),
            "full_image": data["image"][0] if data.get("image") else None,
            "published_at": data.get("datePublished"),
        }
    except Exception as exc:
        log.debug("jsonld enrich failed for %s: %s", article_url, exc)
        return {}
    finally:
        await detail.close()


# Listing DOM first, then merge JSON-LD author/image for the same N cards.
async def scrape_entertainment(page: Page, context: BrowserContext, count: int = 5) -> list[Article]:
    log = logging.getLogger(__name__)
    await page.goto("https://ekantipur.com/entertainment", wait_until="domcontentloaded")
    await page.wait_for_selector("div.category-inner-wrapper", state="attached")
    category_label = (
        await page.locator("header .category-name a").first.inner_text()
    ).strip()
    cards = await page.locator("div.category-inner-wrapper").all()
    articles: list[Article] = []
    article_urls: list[str] = []

    for idx, card in enumerate(cards[:count]):
        try:
            title_link = card.locator(".category-description h2 a").first
            title = (await title_link.inner_text()).strip()
            href = await title_link.get_attribute("href")
            article_url = urljoin(ENTERTAINMENT_URL, href or "")
            author_texts = await card.locator(".author-name a").all_inner_texts()
            if author_texts:
                author = ", ".join(t.strip() for t in author_texts if t.strip())
                author = author or None
            else:
                author = None
            raw_image = await card.locator(".category-image img").first.get_attribute(
                "data-src"
            )
            image_url = unwrap_cdn_url(raw_image or "")

            articles.append(
                Article(
                    title=title,
                    image_url=image_url,
                    category=category_label,
                    author=author,
                )
            )
            article_urls.append(article_url)
        except Exception as exc:
            log.warning("entertainment card %s failed: %s", idx, exc)

    # Limit parallel detail fetches so the site (and local RAM) are not hammered.
    sem = asyncio.Semaphore(3)

    async def bounded_enrich(u: str) -> dict:
        async with sem:
            return await enrich_from_jsonld(context, u)

    extras = await asyncio.gather(*(bounded_enrich(u) for u in article_urls))

    # Prefer structured data over listing text when it is present and non-empty.
    merged: list[Article] = []
    for art, extra in zip(articles, extras, strict=True):
        author = art.author
        image_url = art.image_url
        canonical_author = extra.get("canonical_author")
        if canonical_author is not None and str(canonical_author).strip():
            author = str(canonical_author).strip()
        full_image = extra.get("full_image")
        if full_image is not None:
            candidate: str | None
            if isinstance(full_image, str):
                candidate = full_image.strip()
            elif isinstance(full_image, dict):
                u = full_image.get("url") or full_image.get("contentUrl")
                candidate = str(u).strip() if u else None
            else:
                candidate = str(full_image).strip() or None
            if candidate:
                image_url = unwrap_cdn_url(candidate)
        merged.append(
            Article(
                title=art.title,
                image_url=image_url,
                category=art.category,
                author=author,
            )
        )

    return merged


# Today's cartoon: prefer direct fancybox href, else lazy img data-src.
async def scrape_cartoon(page: Page) -> Cartoon:
    log = logging.getLogger(__name__)
    try:
        await page.goto("https://ekantipur.com/cartoon", wait_until="domcontentloaded")
        await page.wait_for_selector("div.cartoon-wrapper", state="attached")
        first = page.locator("div.cartoon-wrapper").first

        image_url = await first.locator(".cartoon-image figure a").first.get_attribute(
            "href"
        )
        if not (image_url and image_url.strip()):
            data_src = await first.locator(".cartoon-image img").first.get_attribute(
                "data-src"
            )
            image_url = (data_src or "").strip()
        else:
            image_url = image_url.strip()

        title = (await first.locator(".cartoon-description p").first.inner_text()).strip()
        # Site sometimes appends a dash separator; strip iteratively until stable.
        while True:
            if title.endswith(" - "):
                title = title[:-3].strip()
            elif title.endswith(" -"):
                title = title[:-2].strip()
            elif title.endswith("- "):
                title = title[:-2].strip()
            else:
                break

        author = None  # The cartoonist (Abin Shrestha, series 'गजब छ बा') is not present in the DOM, image EXIF, or XMP metadata. Returning None per the assignment's null-when-missing pattern.

        return Cartoon(
            title=title, image_url=unwrap_cdn_url(image_url), author=author
        )
    except Exception as exc:
        log.error("cartoon scrape failed: %s", exc)
        return Cartoon(title="", image_url="", author=None)


# Wire Playwright + CLI flags, run both scrapers, emit JSON with Unicode preserved.
async def main() -> None:
    parser = argparse.ArgumentParser(description="ekantipur scraper")
    parser.add_argument("--headless", default=True, action=BooleanOptionalAction, help="Run browser in headless mode (default: True, use --no-headless to disable)")
    parser.add_argument("--output", default="output.json", help="Path to output JSON file (default: output.json)")
    parser.add_argument("--count", type=int, default=5, help="Number of articles to scrape (default: 5)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose (DEBUG) logging")
    args = parser.parse_args()

    # Verbose must run before basicConfig so the root logger level sticks.
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    configure_logging()
    log = logging.getLogger(__name__)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)
        context = await browser.new_context(user_agent=CHROME_USER_AGENT)
        page = await context.new_page()

        # Same context shares cookies/UA; entertainment uses `page`, cartoon reuses it.
        try:
            entertainment_news = await scrape_entertainment(page, context, args.count)
            cartoon_of_the_day = await scrape_cartoon(page)

            scraped_at = datetime.now(timezone.utc).isoformat()
            # _meta is runtime metadata only; not mixed into article rows.
            result = ScrapeResult(
                entertainment_news=entertainment_news,
                cartoon_of_the_day=cartoon_of_the_day,
                **{
                    "_meta": {
                        "scraped_at": scraped_at,
                        "source_url": ENTERTAINMENT_URL,
                        "scraper_version": SCRAPER_VERSION,
                        "articles_count": len(entertainment_news),
                    }
                },
            )

            payload = result.model_dump(by_alias=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            log.info("Wrote %s (%d articles)", args.output, len(entertainment_news))
        finally:
            await browser.close()


# `python scraper.py` or import `main` and `asyncio.run(main())` from elsewhere.
if __name__ == "__main__":
    asyncio.run(main())