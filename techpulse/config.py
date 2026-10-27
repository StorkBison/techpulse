from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration, loaded from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/techpulse.db"

    # Scraper
    scraper_source: Literal["live", "fixture"] = "live"
    scraper_fixture_path: Path = Path("tests/fixtures/hn_front_page.html")
    scraper_pages: int = Field(default=1, ge=1, le=5)
    scraper_timeout_ms: int = 20_000
    browser_channel: str | None = None  # use an installed Chrome/Edge instead of bundled

    # Scheduler
    schedule_enabled: bool = True
    schedule_interval_minutes: int = Field(default=30, ge=1)

    # AI enrichment
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-5-5"
    interests: str = "AI/LLMs, automation, Python, developer tools, DevOps, security"

    # Alerts
    alert_threshold: int = Field(default=8, ge=1, le=10)
    slack_webhook_url: str | None = None

    @property
    def interest_list(self) -> list[str]:
        return [i.strip() for i in self.interests.split(",") if i.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
