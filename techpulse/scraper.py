"""Hacker News scraper built on Playwright.

The same in-browser extraction runs against the live site or a saved HTML fixture,
so tests and offline demos exercise the real parsing code path.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

HN_BASE = "https://news.ycombinator.com"

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


async def extract_stories(page: Page) -> list[ScrapedStory]:
    raw = await page.evaluate(EXTRACT_JS)
    return [s for s in map(_normalize, raw) if s]


class HackerNewsScraper:
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

    @property
    def source(self) -> str:
        return f"fixture:{self.fixture.name}" if self.fixture else "live"

    async def scrape(self) -> list[ScrapedStory]:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, channel=self.browser_channel)
            try:
                context = await browser.new_context(
                    user_agent="TechPulse/1.0 (+https://github.com/techpulse demo)"
                )
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
                    return await extract_stories(page)

                stories: dict[int, ScrapedStory] = {}
                for n in range(1, self.pages + 1):
                    for story in await self._scrape_page(page, n):
                        stories.setdefault(story.hn_id, story)
                return list(stories.values())
            finally:
                await browser.close()

    @retry(
        retry=retry_if_exception_type(PlaywrightError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    async def _scrape_page(self, page: Page, n: int) -> list[ScrapedStory]:
        log.info("Scraping HN page %d", n)
        await page.goto(f"{HN_BASE}/news?p={n}", wait_until="domcontentloaded")
        await page.wait_for_selector("tr.athing")
        return await extract_stories(page)
