import time


def _run_and_wait(client):
    assert client.post("/api/runs").status_code == 202
    for _ in range(100):
        if not client.get("/api/stats").json()["pipeline_busy"]:
            return
        time.sleep(0.05)
    raise AssertionError("pipeline did not finish")


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_dashboard_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "TechPulse" in res.text


def test_empty_state(client):
    stats = client.get("/api/stats").json()
    assert stats["total_stories"] == 0
    assert stats["last_run"] is None
    assert client.get("/api/stories").json() == []


def test_trigger_run_then_query(client):
    _run_and_wait(client)

    stats = client.get("/api/stats").json()
    assert stats["total_stories"] == 4
    assert stats["last_run"]["status"] == "success"
    assert stats["enricher"] == "keyword-fallback"

    stories = client.get("/api/stories").json()
    relevances = [s["relevance"] for s in stories]
    assert relevances == sorted(relevances, reverse=True)

    ai = client.get("/api/stories", params={"category": "AI/ML"}).json()
    assert ai and all(s["category"] == "AI/ML" for s in ai)

    hits = client.get("/api/stories", params={"q": "kubernetes"}).json()
    assert [s["hn_id"] for s in hits] == [2]

    assert client.get("/api/stories/1").json()["hn_id"] == 1
    assert client.get("/api/stories/12345").status_code == 404
    run = client.get("/api/runs").json()[0]
    assert run["trigger"] == "manual"
    # Timestamps must carry a UTC offset so browsers don't parse them as local time.
    assert run["started_at"].endswith(("Z", "+00:00"))
    assert stories[0]["first_seen"].endswith(("Z", "+00:00"))


def test_validation_errors(client):
    assert client.get("/api/stories", params={"min_relevance": 11}).status_code == 422
    assert client.get("/api/stories", params={"sort": "nope"}).status_code == 422
