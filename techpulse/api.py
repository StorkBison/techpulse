import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from techpulse import __version__
from techpulse.config import Settings, get_settings
from techpulse.db import Database
from techpulse.enricher import build_enricher
from techpulse.models import Run, Story
from techpulse.notifier import Notifier
from techpulse.pipeline import Pipeline, PipelineBusyError
from techpulse.schemas import RunOut, Stats, StoryOut, TriggerResponse
from techpulse.scraper import build_scraper

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
JOB_ID = "scrape-hn"


def build_pipeline(settings: Settings, db: Database) -> Pipeline:
    scraper = build_scraper(
        engine=settings.resolved_scraper_engine,
        pages=settings.scraper_pages,
        timeout_ms=settings.scraper_timeout_ms,
        fixture=settings.scraper_fixture_path if settings.scraper_source == "fixture" else None,
        browser_channel=settings.browser_channel,
    )
    return Pipeline(
        session_factory=db.session,
        scrape=scraper.scrape,
        enricher=build_enricher(
            settings.anthropic_api_key, settings.claude_model, settings.interest_list
        ),
        notifier=Notifier(settings.slack_webhook_url),
        alert_threshold=settings.alert_threshold,
        scraper_engine=scraper.engine,
    )


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_url)
    pipeline = pipeline or build_pipeline(settings, db)
    scheduler = AsyncIOScheduler(timezone="UTC")
    background: set[asyncio.Task] = set()

    async def scheduled_run() -> None:
        try:
            await pipeline.run("schedule")
        except PipelineBusyError:
            log.info("Skipping scheduled run: pipeline busy")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.create_all()
        if settings.schedule_enabled:
            scheduler.add_job(
                scheduled_run, "interval", id=JOB_ID,
                minutes=settings.schedule_interval_minutes,
                max_instances=1, coalesce=True,
            )  # fmt: skip
            scheduler.start()
            log.info("Scheduler started: every %d min", settings.schedule_interval_minutes)
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="TechPulse",
        version=__version__,
        description="AI-curated tech news monitor - scrape, enrich with Claude, alert.",
        lifespan=lifespan,
    )
    app.state.pipeline = pipeline
    app.state.db = db
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    DB = Annotated[Session, Depends(db.session_dependency)]

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", {"version": __version__})

    @app.get("/health", tags=["ops"])
    def health(session: DB):
        session.execute(select(1))
        return {"status": "ok", "version": __version__}

    @app.get("/api/stories", response_model=list[StoryOut], tags=["stories"])
    def list_stories(
        session: DB,
        category: str | None = None,
        min_relevance: Annotated[int, Query(ge=1, le=10)] = 1,
        q: Annotated[str | None, Query(max_length=100)] = None,
        sort: Literal["relevance", "points", "recent"] = "relevance",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        stmt = select(Story).where(func.coalesce(Story.relevance, 0) >= min_relevance)
        if category:
            stmt = stmt.where(Story.category == category)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Story.title.ilike(like), Story.summary.ilike(like)))
        order = {
            "relevance": (Story.relevance.desc(), Story.points.desc()),
            "points": (Story.points.desc(),),
            "recent": (Story.first_seen.desc(), Story.rank.asc()),
        }[sort]
        return session.scalars(stmt.order_by(*order).limit(limit)).all()

    @app.get("/api/stories/{hn_id}", response_model=StoryOut, tags=["stories"])
    def get_story(hn_id: int, session: DB):
        story = session.scalar(select(Story).where(Story.hn_id == hn_id))
        if not story:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Story not found")
        return story

    @app.get("/api/runs", response_model=list[RunOut], tags=["runs"])
    def list_runs(session: DB, limit: Annotated[int, Query(ge=1, le=100)] = 20):
        return session.scalars(select(Run).order_by(Run.id.desc()).limit(limit)).all()

    @app.post(
        "/api/runs",
        response_model=TriggerResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["runs"],
        responses={
            409: {"description": "A run is already in progress"},
            429: {"description": "Cooldown since the last run has not elapsed"},
        },
    )
    async def trigger_run(session: DB):
        if pipeline.busy:
            raise HTTPException(status.HTTP_409_CONFLICT, "A pipeline run is already in progress")
        enforce_cooldown(session)
        if settings.serverless:
            # No work may outlive the response on serverless, so run inline.
            run = await pipeline.run("manual")
            return TriggerResponse(status=run.status, message=f"Run #{run.id} {run.status}")
        task = asyncio.create_task(pipeline.run("manual"))
        background.add(task)
        task.add_done_callback(background.discard)
        return TriggerResponse(status="started", message="Pipeline run started")

    def enforce_cooldown(session: Session) -> None:
        """Public deployments: stop the Run button from being used to hammer HN / the AI API."""
        cooldown = settings.manual_run_cooldown_seconds
        if not cooldown:
            return
        last = session.scalar(select(Run.started_at).order_by(Run.id.desc()).limit(1))
        if last is None:
            return
        elapsed = (
            datetime.now(UTC) - (last if last.tzinfo else last.replace(tzinfo=UTC))
        ).total_seconds()
        if elapsed < cooldown:
            wait = int(cooldown - elapsed) + 1
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Please wait {wait}s - runs are limited to one every {cooldown}s",
                headers={"Retry-After": str(wait)},
            )

    @app.get(
        "/api/cron",
        response_model=RunOut,
        tags=["runs"],
        responses={401: {"description": "Missing or wrong CRON_SECRET"}},
    )
    async def cron_run(authorization: Annotated[str | None, Header()] = None):
        """Scheduled entrypoint for Vercel Cron / external schedulers (Bearer CRON_SECRET)."""
        expected = f"Bearer {settings.cron_secret}" if settings.cron_secret else None
        if not expected or not hmac.compare_digest(authorization or "", expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized")
        try:
            return await pipeline.run("cron")
        except PipelineBusyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    @app.get("/api/stats", response_model=Stats, tags=["ops"])
    def stats(session: DB):
        total = session.scalar(select(func.count(Story.id))) or 0
        enriched = session.scalar(select(func.count(Story.id)).where(Story.relevance.is_not(None)))
        alerts = session.scalar(select(func.count(Story.id)).where(Story.alerted.is_(True)))
        avg = session.scalar(select(func.avg(Story.relevance)))
        by_category = dict(
            session.execute(
                select(Story.category, func.count(Story.id))
                .where(Story.category.is_not(None))
                .group_by(Story.category)
                .order_by(func.count(Story.id).desc())
            ).all()
        )
        last_run = session.scalar(select(Run).order_by(Run.id.desc()).limit(1))
        job = scheduler.get_job(JOB_ID) if scheduler.running else None
        return Stats(
            total_stories=total,
            enriched_stories=enriched or 0,
            alerts_sent=alerts or 0,
            avg_relevance=round(float(avg), 1) if avg is not None else None,
            by_category=by_category,
            last_run=last_run,
            next_run_at=job.next_run_time if job else None,
            pipeline_busy=pipeline.busy,
            enricher=pipeline.enricher.name,
            scraper_source=f"{pipeline.scraper_engine} / {settings.scraper_source}",
            schedule_label=settings.schedule_label
            or (None if settings.schedule_enabled else "external cron"),
            storage="ephemeral (per instance)"
            if settings.ephemeral_database
            else settings.database_url.split(":", 1)[0].split("+", 1)[0],
            alert_channel=pipeline.notifier.channel,
            alert_threshold=pipeline.alert_threshold,
        )

    return app
