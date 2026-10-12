"""Request security contracts using a tiny SQLite cache, never the production app."""
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT))
from routes import api_briefing as route
import models


@pytest.fixture
def cache_client(tmp_path, monkeypatch):
    path = tmp_path / "users.db"
    def connect():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return con
    con = connect()
    con.execute("CREATE TABLE briefing_cache (cache_key TEXT PRIMARY KEY, briefing TEXT, "
                "article_count INTEGER, generated_at TEXT, sources_json TEXT, meta_json TEXT)")
    stamp = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    con.execute("INSERT INTO briefing_cache VALUES (?, ?, ?, ?, ?, ?)",
                ("_all:24", "A cached sourced update [1].", 2, stamp,
                 json.dumps([{"n": 1, "title": "A source", "link": "https://example.org"}]), "{}"))
    con.commit()
    con.close()
    monkeypatch.setattr(models, "get_user_db", connect)
    def unexpected(*args, **kwargs):
        pytest.fail("Public request crossed the generation boundary")
    for name in ("_fetch_briefing_events", "_editor_pass", "_generate_briefing", "get_threads"):
        monkeypatch.setattr(route, name, unexpected)
    route._regen_in_flight.clear()
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(route.bp)
    return app.test_client(), app, connect


def test_public_stale_cache_is_read_only(cache_client):
    client, _, _ = cache_client
    response = client.get("/api/briefing?hours=24")
    assert response.status_code == 200
    assert response.json["cached"] and response.json["stale"]
    assert "cached sourced update" in response.json["briefing"]


def test_public_cache_miss_never_generates(cache_client):
    client, _, _ = cache_client
    response = client.get("/api/briefing?hours=3")
    assert response.status_code == 503
    assert response.json["briefing"] is None


@pytest.mark.parametrize("flag", ["refresh", "prewarm"])
def test_get_cannot_force_generation(cache_client, flag):
    client, _, _ = cache_client
    assert client.get(f"/api/briefing?{flag}=1").status_code == 403


@pytest.mark.parametrize("query", ["hours=-1", "hours=100000", "hours=nope", "view=" + "x"*81,
                                         "view=../x", "hours=3&hours=24"])
def test_invalid_cache_keys_rejected_before_storage(cache_client, monkeypatch, query):
    client, _, _ = cache_client
    def no_db():
        pytest.fail("Invalid key reached database")
    monkeypatch.setattr(models, "get_user_db", no_db)
    assert client.get("/api/briefing?" + query).status_code == 400


def test_public_sse_serves_cached_text_once(cache_client):
    client, _, _ = cache_client
    response = client.get("/api/briefing?hours=24&stream=1")
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert sum("text" in e for e in events) == 1
    assert events[-1]["done"] is True
    assert events[-1]["stale"] is True
    assert not route._regen_in_flight


def test_worker_token_required(cache_client, monkeypatch, tmp_path):
    client, _, _ = cache_client
    import briefing_access
    monkeypatch.setattr(briefing_access, "TOKEN_PATH", tmp_path / "absent")
    monkeypatch.delenv("GDELT_BRIEFING_WORKER_TOKEN", raising=False)
    assert client.post("/api/briefing?prewarm=1").status_code == 403
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "test-worker-token")
    assert client.post("/api/briefing?prewarm=1", headers={"X-GDELT-Briefing-Token": "wrong"}).status_code == 403
    assert client.post("/api/briefing?prewarm=1", headers={"X-GDELT-Briefing-Token": "é"}).status_code == 403


def test_worker_generates_and_releases_slot(cache_client, monkeypatch):
    client, _, _ = cache_client
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "test-worker-token")
    monkeypatch.setattr(route, "get_threads", lambda key: [])
    monkeypatch.setattr(route, "_fetch_briefing_events", lambda *a: ([{"n": 1}, {"n": 2}], "Global", "News"))
    monkeypatch.setattr(route, "_editor_pass", lambda sources, *a: (sources, [], {}))
    monkeypatch.setattr(route, "_build_briefing_prompt", lambda *a, **k: "synthetic prompt")
    monkeypatch.setattr(route, "_generate_briefing", lambda *a, **k: "Synthetic update [1].")
    monkeypatch.setattr(route, "_normalize_briefing", lambda text, *a: text)
    monkeypatch.setattr(route, "record_briefing_history", lambda *a, **k: None)
    monkeypatch.setattr(route, "should_update_threads", lambda *a: False)
    headers = {"X-GDELT-Briefing-Token": "test-worker-token"}
    response = client.post("/api/briefing?hours=24&prewarm=1", headers=headers)
    assert response.status_code == 200 and not response.json["cached"]
    assert response.json["briefing"] == "Synthetic update [1]."
    assert not route._regen_in_flight
    # Streamed worker generation is disallowed, so a disconnected client cannot leak a slot.
    assert client.post("/api/briefing?prewarm=1&stream=1", headers=headers).status_code == 400
    assert client.post("/api/briefing?prewarm=1&view=unknown", headers=headers).status_code == 400


def test_worker_failure_does_not_leak_slot(cache_client, monkeypatch):
    client, _, _ = cache_client
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "test-worker-token")
    def fail(*a):
        raise RuntimeError("synthetic fetch failure")
    monkeypatch.setattr(route, "get_threads", lambda key: [])
    monkeypatch.setattr(route, "_fetch_briefing_events", fail)
    with pytest.raises(RuntimeError, match="synthetic fetch"):
        client.post("/api/briefing?prewarm=1", headers={"X-GDELT-Briefing-Token": "test-worker-token"})
    assert not route._regen_in_flight


def test_generation_concurrency_is_global():
    route._regen_in_flight.clear()
    try:
        assert route._try_start_regen("a:3")
        assert not route._try_start_regen("b:24")
    finally:
        route._regen_in_flight.clear()


def test_prewarm_posts_host_local_token_without_logging_it(monkeypatch, capsys):
    from pipeline import prewarm_briefings as prewarm
    monkeypatch.setenv("GDELT_BRIEFING_WORKER_TOKEN", "synthetic-secret")
    class Result:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def read(self):
            return b'{"briefing":"A sourced update", "cached":true}'
    def request(req, timeout):
        assert req.method == "POST"
        assert dict((k.lower(), v) for k, v in req.header_items())["x-gdelt-briefing-token"] == "synthetic-secret"
        assert "synthetic-secret" not in req.full_url
        return Result()
    monkeypatch.setattr(prewarm.urllib.request, "urlopen", request)
    assert prewarm.warm("", 24, "http://localhost:1234") is True
    assert "synthetic-secret" not in capsys.readouterr().out


def test_failed_prewarm_does_not_advance_version(monkeypatch, tmp_path):
    from pipeline import prewarm_briefings as prewarm
    monkeypatch.setattr(sys, "argv", ["prewarm_briefings.py"])
    monkeypatch.setattr(prewarm, "current_version", lambda: "new-version")
    monkeypatch.setattr(prewarm, "curated_combos", lambda: [("", 24)])
    monkeypatch.setattr(prewarm, "cache_plan", lambda *a: [])
    monkeypatch.setattr(prewarm, "warm", lambda *a: None)
    monkeypatch.setattr(prewarm, "STAGGER_S", 0)
    marker = tmp_path / "version"
    marker.write_text("old-version")
    monkeypatch.setattr(prewarm, "LAST_PREWARM_FILE", marker)
    assert prewarm.main() == 1
    assert marker.read_text() == "old-version"
