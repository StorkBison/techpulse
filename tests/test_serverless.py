"""Behaviour specific to serverless deployments (Vercel)."""

import pytest
from fastapi.testclient import TestClient

from techpulse.api import create_app
from techpulse.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        **{"serverless": True, "cron_secret": "s3cret-value-123", **overrides},
    )


@pytest.fixture
def serverless_client(tmp_path, make_pipeline):
    app = create_app(make_settings(tmp_path), pipeline=make_pipeline())
    with TestClient(app) as c:
        yield c


def test_serverless_defaults(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    s = Settings(_env_file=None)
    assert s.serverless
    assert s.schedule_enabled is False
    assert s.database_url == "sqlite:////tmp/techpulse.db"
    assert s.ephemeral_database
    assert s.resolved_scraper_engine == "http"


@pytest.mark.parametrize(
    "url",
    ["postgres://u:p@host/db?sslmode=require", "postgresql://u:p@host/db?sslmode=require"],
)
def test_postgres_urls_use_psycopg_driver(url):
    s = Settings(_env_file=None, database_url=url)
    assert s.database_url == "postgresql+psycopg://u:p@host/db?sslmode=require"


def test_cron_requires_secret(serverless_client):
    assert serverless_client.get("/api/cron").status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert serverless_client.get("/api/cron", headers=bad).status_code == 401


def test_cron_runs_pipeline(serverless_client):
    res = serverless_client.get("/api/cron", headers={"Authorization": "Bearer s3cret-value-123"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success" and body["trigger"] == "cron" and body["scraped"] == 4


def test_cron_disabled_without_configured_secret(tmp_path, make_pipeline):
    app = create_app(make_settings(tmp_path, cron_secret=None), pipeline=make_pipeline())
    with TestClient(app) as c:
        assert c.get("/api/cron", headers={"Authorization": "Bearer "}).status_code == 401


def test_manual_run_is_inline_on_serverless(serverless_client):
    res = serverless_client.post("/api/runs")
    assert res.status_code == 202
    assert res.json()["status"] == "success"  # finished before the response
    stats = serverless_client.get("/api/stats").json()
    assert stats["total_stories"] == 4
    assert stats["schedule_label"] == "external cron"


def test_manual_run_cooldown(tmp_path, make_pipeline):
    settings = make_settings(tmp_path, manual_run_cooldown_seconds=300)
    with TestClient(create_app(settings, pipeline=make_pipeline())) as c:
        assert c.post("/api/runs").status_code == 202
        res = c.post("/api/runs")
        assert res.status_code == 429
        assert int(res.headers["Retry-After"]) > 0
