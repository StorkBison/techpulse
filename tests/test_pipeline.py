import asyncio

import pytest
from sqlalchemy import func, select

from techpulse.models import Story
from techpulse.pipeline import PipelineBusyError
from tests.conftest import FakeScraper, make_story


async def test_run_scrapes_enriches_and_alerts(db, make_pipeline, notifier):
    run = await make_pipeline(threshold=6).run("test")

    assert run.status == "success", run.error
    assert (run.scraped, run.new, run.enriched) == (4, 4, 4)
    assert run.alerts == len(notifier.sent) > 0
    assert all(s.relevance >= 6 for s in notifier.sent)
    with db.session() as s:
        assert s.scalar(select(func.count(Story.id)).where(Story.relevance.is_(None))) == 0


async def test_second_run_is_idempotent_and_refreshes_metrics(db, make_pipeline, notifier):
    scraper = FakeScraper()
    pipeline = make_pipeline(scraper=scraper, threshold=6)
    await pipeline.run("test")
    alerts_after_first = len(notifier.sent)

    scraper.stories = [make_story(1, "Building reliable AI agents with Claude and Python", 999)]
    run = await pipeline.run("test")

    assert (run.new, run.enriched, run.alerts) == (0, 0, 0)
    assert len(notifier.sent) == alerts_after_first  # no duplicate alerts
    with db.session() as s:
        assert s.scalar(select(Story.points).where(Story.hn_id == 1)) == 999


async def test_failed_scrape_is_recorded_not_raised(make_pipeline):
    run = await make_pipeline(scraper=FakeScraper(error=TimeoutError("hn down"))).run("test")
    assert run.status == "failed"
    assert "hn down" in run.error
    assert run.finished_at is not None


async def test_enrichment_failure_keeps_stories_for_retry(db, make_pipeline):
    class Broken:
        name = "broken"

        def enrich(self, stories):
            raise ConnectionError("api unavailable")

    run = await make_pipeline(enricher=Broken()).run("test")
    assert run.status == "failed"
    with db.session() as s:  # scraped rows were committed; next run will enrich them
        assert s.scalar(select(func.count(Story.id))) == 4

    retry = await make_pipeline().run("test")
    assert retry.enriched == 4


async def test_concurrent_runs_are_rejected(make_pipeline):
    gate = asyncio.Event()

    class SlowScraper(FakeScraper):
        async def __call__(self):
            await gate.wait()
            return []

    pipeline = make_pipeline(scraper=SlowScraper())
    first = asyncio.create_task(pipeline.run("a"))
    await asyncio.sleep(0)
    assert pipeline.busy
    with pytest.raises(PipelineBusyError):
        await pipeline.run("b")
    gate.set()
    assert (await first).status == "success"
