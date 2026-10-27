from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from techpulse.api import create_app
from techpulse.config import Settings
from techpulse.db import Database
from techpulse.enricher import KeywordEnricher
from techpulse.notifier import Notifier
from techpulse.pipeline import Pipeline
from techpulse.scraper import ScrapedStory

FIXTURE = Path(__file__).parent / "fixtures" / "hn_front_page.html"


def make_story(hn_id: int, title: str, points: int = 100, domain: str = "example.com"):
    return ScrapedStory(
        hn_id=hn_id, rank=hn_id, title=title, url=f"https://{domain}/{hn_id}",
        domain=domain, author="alice", points=points, comments=10,
    )  # fmt: skip


SAMPLE = [
    make_story(1, "Building reliable AI agents with Claude and Python", 420),
    make_story(2, "A new Kubernetes security vulnerability explained", 210),
    make_story(3, "The history of the bicycle", 90),
    make_story(4, "Show HN: A Rust compiler for tiny devices", 150),
]


class FakeScraper:
    def __init__(self, stories=None, error: Exception | None = None):
        self.stories = list(stories if stories is not None else SAMPLE)
        self.error = error
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.stories


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__(None)
        self.sent = []

    def send(self, stories):
        self.sent.extend(stories)
        return len(stories)


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_all()
    return database


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def make_pipeline(db, notifier):
    def _make(scraper=None, enricher=None, threshold=8):
        return Pipeline(
            session_factory=db.session,
            scrape=scraper or FakeScraper(),
            enricher=enricher or KeywordEnricher(["AI", "Python", "security", "DevOps"]),
            notifier=notifier,
            alert_threshold=threshold,
        )

    return _make


@pytest.fixture
def client(tmp_path, make_pipeline):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        schedule_enabled=False,
        _env_file=None,
    )
    app = create_app(settings, pipeline=make_pipeline())
    with TestClient(app) as c:
        yield c
