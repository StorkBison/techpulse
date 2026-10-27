"""Both scraper engines run their real parsing code against a saved Hacker News page."""

import os

import pytest

from techpulse.scraper import BrowserScraper, HttpScraper, build_scraper
from tests.conftest import FIXTURE


def _check(stories):
    assert len(stories) == 30
    assert len({s.hn_id for s in stories}) == 30
    assert [s.rank for s in stories] == list(range(1, 31))
    first = stories[0]
    assert first.title and first.points > 0 and first.author
    # Self-posts (Ask HN etc.) get absolute URLs
    assert all(s.url.startswith("http") for s in stories)


async def test_http_engine_parses_fixture():
    _check(await HttpScraper(fixture=FIXTURE).scrape())


@pytest.mark.browser
async def test_browser_engine_parses_fixture():
    _check(
        await BrowserScraper(fixture=FIXTURE, browser_channel=os.getenv("BROWSER_CHANNEL")).scrape()
    )


@pytest.mark.browser
async def test_engines_agree():
    browser = await BrowserScraper(
        fixture=FIXTURE, browser_channel=os.getenv("BROWSER_CHANNEL")
    ).scrape()
    assert await HttpScraper(fixture=FIXTURE).scrape() == browser


def test_build_scraper_selects_engine():
    assert build_scraper("http", 1, 1000, None, None).engine == "http"
    assert build_scraper("browser", 1, 1000, None, "chrome").engine == "browser"
