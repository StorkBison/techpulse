"""Runs the real Playwright extraction against a saved Hacker News page."""

import os

import pytest

from techpulse.scraper import HackerNewsScraper
from tests.conftest import FIXTURE

pytestmark = pytest.mark.browser


async def test_extracts_stories_from_fixture():
    stories = await HackerNewsScraper(
        fixture=FIXTURE, browser_channel=os.getenv("BROWSER_CHANNEL")
    ).scrape()

    assert len(stories) == 30
    assert len({s.hn_id for s in stories}) == 30
    assert [s.rank for s in stories] == list(range(1, 31))
    first = stories[0]
    assert first.title and first.url.startswith("http")
    assert first.points > 0 and first.author
    # Self-posts (Ask HN etc.) get absolute URLs
    assert all(s.url.startswith("http") for s in stories)
