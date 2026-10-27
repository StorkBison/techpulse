"""Hacker News scrapers.

Two interchangeable engines produce identical `ScrapedStory` records:

- `BrowserScraper` drives headless Chromium with Playwright and extracts in-page.
- `HttpScraper` fetches the HTML with httpx and parses it server-side. HN pages are
  static, so this needs no browser - used on serverless platforms (e.g. Vercel).

Both can replay a saved HTML fixture, so tests and offline demos exercise the real
parsing code paths.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from bs4 import BeautifulSoup
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

HN_BASE = "https://news.ycombinator.com"
USER_AGENT = "TechPulse/1.0 (+https://github.com/StorkBison/techpulse)"


class ScrapeError(RuntimeError):
    """Transient scrape failure - retried with backoff."""


_retry = retry(
    retry=retry_if_exception_type(ScrapeError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


@dataclass(frozen=True)
class ScrapedStory:
    hn_id: int
    rank: int
    title: str
    url: str
    domain: str | None
    author: str | None
    points: int
    comments: int


class Scraper(Protocol):
    engine: str

    async def scrape(self) -> list[ScrapedStory]: ...


def _normalize(raw: dict) -> ScrapedStory | None:
    if not raw.get("hn_id") or not raw.get("title"):
        return None
    url = raw.get("url") or ""
    if url.startswith("item?"):  # Ask HN / Show HN self-posts
        url = f"{HN_BASE}/{url}"
    return ScrapedStory(
        hn_id=int(raw["hn_id"]),
        rank=int(raw.get("rank") or 0),
        title=raw["title"],
        url=url,
        domain=raw.get("domain"),
        author=raw.get("author"),
        points=int(raw.get("points") or 0),
        comments=int(raw.get("comments") or 0),
    )


def _dedupe(pages: list[list[ScrapedStory]]) -> list[ScrapedStory]:
    stories: dict[int, ScrapedStory] = {}
    for page in pages:
        for story in page:
            stories.setdefault(story.hn_id, story)
    return list(stories.values())


# --- Browser engine ------------------------------------------------------------

# Runs inside the page. HN renders each story as two rows: tr.athing (title) + the
# following row (subline with score/author/comments). Job posts have no score.
EXTRACT_JS = """
() => Array.from(document.querySelectorAll('tr.athing')).map(row => {
  const link = row.querySelector('.titleline > a');
  const sub = row.nextElementSibling;
  const num = (el) => el ? parseInt(el.textContent.replace(/\\D+/g, ''), 10) || 0 : 0;
  const commentsLink = sub ? Array.from(sub.querySelectorAll('a'))
      .find(a => /comment|discuss/.test(a.textContent)) : null;
  return {
    hn_id: parseInt(row.id, 10),
    rank: num(row.querySelector('.rank')),
    title: link ? link.textContent.trim() : '',
    url: link ? link.getAttribute('href') : '',
    domain: (row.querySelector('.sitestr') || {}).textContent || null,
    author: sub ? ((sub.querySelector('.hnuser') || {}).textContent || null) : null,
    points: sub ? num(sub.querySelector('.score')) : 0,
    comments: commentsLink && /\\d/.test(commentsLink.textContent) ? num(commentsLink) : 0,
  };
})
"""


class BrowserScraper:
    engine = "browser"

    def __init__(
        self,
        pages: int = 1,
        timeout_ms: int = 20_000,
        fixture: Path | None = None,
        browser_channel: str | None = None,
    ):
        self.pages = pages
        self.timeout_ms = timeout_ms
        self.fixture = fixture
        self.browser_channel = browser_channel  # e.g. "chrome" / "msedge"; None = bundled

    async def scrape(self) -> list[ScrapedStory]:
        # Imported lazily: Playwright is an optional extra (not installed on serverless).
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, channel=self.browser_channel)
            try:
                context = await browser.new_context(user_agent=USER_AGENT)
                page = await context.new_page()
                page.set_default_timeout(self.timeout_ms)
                # Speed: we only need the DOM, not images/fonts/styles.
                await page.route(
                    "**/*",
                    lambda r: (
                        r.abort()
                        if r.request.resource_type in {"image", "font", "stylesheet"}
                        else r.continue_()
                    ),
                )
                if self.fixture:
                    await page.set_content(self.fixture.read_text(encoding="utf-8"))
                    return await self._extract(page)
                return _dedupe([await self._scrape_page(page, n) for n in range(1, self.pages + 1)])
            finally:
                await browser.close()

    @staticmethod
    async def _extract(page) -> list[ScrapedStory]:
        return [s for s in map(_normalize, await page.evaluate(EXTRACT_JS)) if s]

    @_retry
    async def _scrape_page(self, page, n: int) -> list[ScrapedStory]:
        from playwright.async_api import Error as PlaywrightError

        log.info("Scraping HN page %d (browser)", n)
        try:
            await page.goto(f"{HN_BASE}/news?p={n}", wait_until="domcontentloaded")
            await page.wait_for_selector("tr.athing")
        except PlaywrightError as exc:
            raise ScrapeError(str(exc)) from exc
        return await self._extract(page)


# --- HTTP engine ---------------------------------------------------------------

_DIGITS = re.compile(r"\d+")


def _num(text: str | None) -> int:
    match = _DIGITS.search((text or "").replace(",", ""))
    return int(match.group()) if match else 0


def parse_front_page(html: str) -> list[ScrapedStory]:
    """Server-side twin of EXTRACT_JS."""
    soup = BeautifulSoup(html, "html.parser")
    raws = []
    for row in soup.select("tr.athing"):
        link = row.select_one(".titleline > a")
        sub = row.find_next_sibling("tr")
        rank = row.select_one(".rank")
        domain = row.select_one(".sitestr")
        author = sub.select_one(".hnuser") if sub else None
        score = sub.select_one(".score") if sub else None
        comments = next(
            (a for a in (sub.select("a") if sub else []) if re.search("comment|discuss", a.text)),
            None,
        )
        raws.append(
            {
                "hn_id": _num(row.get("id")),
                "rank": _num(rank.get_text()) if rank else 0,
                "title": link.get_text().strip() if link else "",
                "url": link.get("href", "") if link else "",
                "domain": domain.get_text() if domain else None,
                "author": author.get_text() if author else None,
                "points": _num(score.get_text()) if score else 0,
                "comments": _num(comments.get_text()) if comments else 0,
            }
        )
    return [s for s in map(_normalize, raws) if s]


class HttpScraper:
    engine = "http"

    def __init__(self, pages: int = 1, timeout_ms: int = 20_000, fixture: Path | None = None):
        self.pages = pages
        self.timeout = timeout_ms / 1000
        self.fixture = fixture

    async def scrape(self) -> list[ScrapedStory]:
        if self.fixture:
            return parse_front_page(self.fixture.read_text(encoding="utf-8"))
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=self.timeout, follow_redirects=True
        ) as client:
            return _dedupe([await self._scrape_page(client, n) for n in range(1, self.pages + 1)])

    @_retry
    async def _scrape_page(self, client: httpx.AsyncClient, n: int) -> list[ScrapedStory]:
        log.info("Scraping HN page %d (http)", n)
        try:
            response = await client.get(f"{HN_BASE}/news", params={"p": n})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ScrapeError(str(exc)) from exc
        return parse_front_page(response.text)


def build_scraper(
    engine: str, pages: int, timeout_ms: int, fixture: Path | None, browser_channel: str | None
) -> Scraper:
    if engine == "browser":
        return BrowserScraper(pages, timeout_ms, fixture, browser_channel)
    return HttpScraper(pages, timeout_ms, fixture)
