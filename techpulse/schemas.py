from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


def _assume_utc(value: datetime) -> datetime:
    # SQLite drops tzinfo on round-trip; everything is stored in UTC, so restore it
    # (otherwise browsers parse the timestamp as local time).
    return value if value.tzinfo else value.replace(tzinfo=UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_assume_utc)]


class StoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    hn_id: int
    title: str
    url: str
    domain: str | None
    author: str | None
    points: int
    comments: int
    rank: int | None
    first_seen: UTCDateTime
    last_seen: UTCDateTime
    category: str | None
    relevance: int | None
    summary: str | None
    tags: list[str]
    enriched_by: str | None
    alerted: bool


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    status: str
    started_at: UTCDateTime
    finished_at: UTCDateTime | None
    scraped: int
    new: int
    enriched: int
    alerts: int
    enricher: str | None
    error: str | None


class Stats(BaseModel):
    total_stories: int
    enriched_stories: int
    alerts_sent: int
    avg_relevance: float | None
    by_category: dict[str, int]
    last_run: RunOut | None
    next_run_at: UTCDateTime | None
    pipeline_busy: bool
    enricher: str
    scraper_source: str
    alert_channel: str
    alert_threshold: int


class TriggerResponse(BaseModel):
    status: str
    message: str
