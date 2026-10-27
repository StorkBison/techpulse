"""The automation pipeline: scrape -> upsert -> enrich -> alert, recorded as a Run."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from techpulse.enricher import Enricher, StoryInput
from techpulse.models import Run, Story
from techpulse.notifier import Notifier
from techpulse.scraper import ScrapedStory

log = logging.getLogger(__name__)

ENRICH_BATCH_SIZE = 30

ScrapeFn = Callable[[], Awaitable[list[ScrapedStory]]]


class PipelineBusyError(RuntimeError):
    pass


class Pipeline:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        scrape: ScrapeFn,
        enricher: Enricher,
        notifier: Notifier,
        alert_threshold: int = 8,
        scraper_engine: str = "custom",
    ):
        self.session_factory = session_factory
        self.scrape = scrape
        self.enricher = enricher
        self.notifier = notifier
        self.alert_threshold = alert_threshold
        self.scraper_engine = scraper_engine
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def run(self, trigger: str) -> Run:
        """Execute one full pass. Concurrent calls are rejected, not queued."""
        if self._lock.locked():
            raise PipelineBusyError("A pipeline run is already in progress")
        async with self._lock:
            return await self._run(trigger)

    async def _run(self, trigger: str) -> Run:
        with self.session_factory() as db:
            run = Run(trigger=trigger, enricher=self.enricher.name)
            db.add(run)
            db.commit()
            try:
                scraped = await self.scrape()
                run.scraped = len(scraped)
                new_stories = self._upsert(db, scraped)
                run.new = len(new_stories)
                db.commit()

                pending = list(
                    db.scalars(select(Story).where(Story.relevance.is_(None)).order_by(Story.id))
                )
                run.enriched = await self._enrich(pending)
                db.commit()

                run.alerts = self._alert(db)
                run.status = "success"
            except Exception as exc:  # recorded on the run, surfaced in the dashboard
                log.exception("Pipeline run %s failed", run.id)
                db.rollback()
                run.status = "failed"
                run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(UTC)
            db.commit()
            log.info(
                "Run %s %s: scraped=%d new=%d enriched=%d alerts=%d",
                run.id, run.status, run.scraped, run.new, run.enriched, run.alerts,
            )  # fmt: skip
            return run

    @staticmethod
    def _upsert(db: Session, scraped: list[ScrapedStory]) -> list[Story]:
        ids = [s.hn_id for s in scraped]
        existing = {s.hn_id: s for s in db.scalars(select(Story).where(Story.hn_id.in_(ids)))}
        now = datetime.now(UTC)
        created = []
        for s in scraped:
            story = existing.get(s.hn_id)
            if story:  # refresh live metrics, keep enrichment
                story.points, story.comments, story.rank = s.points, s.comments, s.rank
                story.last_seen = now
                continue
            story = Story(
                hn_id=s.hn_id, title=s.title, url=s.url, domain=s.domain, author=s.author,
                points=s.points, comments=s.comments, rank=s.rank, first_seen=now, last_seen=now,
            )  # fmt: skip
            db.add(story)
            created.append(story)
        return created

    async def _enrich(self, stories: list[Story]) -> int:
        by_id = {s.hn_id: s for s in stories}
        done = 0
        for i in range(0, len(stories), ENRICH_BATCH_SIZE):
            batch = stories[i : i + ENRICH_BATCH_SIZE]
            inputs = [StoryInput(hn_id=s.hn_id, title=s.title, domain=s.domain) for s in batch]
            # The enricher does blocking network I/O; keep the event loop free.
            for insight in await asyncio.to_thread(self.enricher.enrich, inputs):
                story = by_id[insight.hn_id]
                story.category = insight.category
                story.relevance = insight.relevance
                story.summary = insight.summary
                story.tags = insight.tags
                story.enriched_by = self.enricher.name
                done += 1
        return done

    def _alert(self, db: Session) -> int:
        due = list(
            db.scalars(
                select(Story)
                .where(Story.relevance >= self.alert_threshold, Story.alerted.is_(False))
                .order_by(Story.relevance.desc(), Story.points.desc())
            )
        )
        sent = self.notifier.send(due)
        for story in due:
            story.alerted = True
        db.commit()
        return sent
