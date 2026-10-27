import importlib.util
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration, loaded from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Serverless platforms (Vercel sets VERCEL=1) have no long-running process, no
    # browser binaries and a read-only filesystem; several defaults adapt to that.
    serverless: bool = Field(default_factory=lambda: os.getenv("VERCEL") == "1")

    database_url: str = ""

    # Scraper
    scraper_engine: Literal["auto", "browser", "http"] = "auto"
    scraper_source: Literal["live", "fixture"] = "live"
    scraper_fixture_path: Path = Path("tests/fixtures/hn_front_page.html")
    scraper_pages: int = Field(default=1, ge=1, le=5)
    scraper_timeout_ms: int = 20_000
    browser_channel: str | None = None  # use an installed Chrome/Edge instead of bundled

    # Scheduler (in-process). Serverless deployments use cron hitting /api/cron instead.
    schedule_enabled: bool = True
    schedule_interval_minutes: int = Field(default=30, ge=1)
    schedule_label: str | None = None  # shown on the dashboard when there is no in-process job
    cron_secret: str | None = None
    manual_run_cooldown_seconds: int = Field(default=0, ge=0)

    # AI enrichment
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-5-5"
    interests: str = "AI/LLMs, automation, Python, developer tools, DevOps, security"

    # Alerts
    alert_threshold: int = Field(default=8, ge=1, le=10)
    slack_webhook_url: str | None = None

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, url: str) -> str:
        # Hosted Postgres providers (Neon, Supabase, Heroku) hand out postgres:// URLs.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    @model_validator(mode="after")
    def _serverless_defaults(self) -> "Settings":
        if not self.database_url:
            # /tmp is the only writable path on serverless; data there is per-instance.
            self.database_url = (
                "sqlite:////tmp/techpulse.db"
                if self.serverless
                else "sqlite:///./data/techpulse.db"
            )
        if self.serverless:
            self.schedule_enabled = False
        return self

    @property
    def interest_list(self) -> list[str]:
        return [i.strip() for i in self.interests.split(",") if i.strip()]

    @property
    def resolved_scraper_engine(self) -> Literal["browser", "http"]:
        if self.scraper_engine != "auto":
            return self.scraper_engine
        has_playwright = importlib.util.find_spec("playwright") is not None
        return "browser" if has_playwright and not self.serverless else "http"

    @property
    def ephemeral_database(self) -> bool:
        return self.serverless and self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
