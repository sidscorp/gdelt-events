"""/admin/metrics: read-only collectors, content-based health, admin-only route.

Never imports dashboard/app.py (it opens the production DuckDB); the route is
mounted on a minimal Flask app with the real template.
"""
import io
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT))

import flask  # noqa: E402  (real Flask before any test stubs it)
import metrics  # noqa: E402


def _ts(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def _users_db(d: Path, with_event_type=True):
    con = sqlite3.connect(d / "users.db")
    et = ", event_type TEXT DEFAULT 'pageview'" if with_event_type else ""
    con.executescript(f"""
        CREATE TABLE pageview_log (id INTEGER PRIMARY KEY, ts TEXT, path TEXT, view_id TEXT, hours INTEGER,
            briefing_key TEXT, ip_hash TEXT, ua TEXT, screen_w INTEGER, referrer TEXT, country TEXT,
            region TEXT, city TEXT, timezone TEXT{et});
        CREATE TABLE briefing_history (id INTEGER PRIMARY KEY, cache_key TEXT, view_id TEXT, hours INTEGER,
            generated_at TEXT, briefing TEXT, article_count INTEGER, sources_json TEXT, meta_json TEXT, trigger TEXT);
        CREATE TABLE briefing_cache (cache_key TEXT PRIMARY KEY, briefing TEXT, article_count INTEGER,
            generated_at TEXT, sources_json TEXT, meta_json TEXT);
    """)
    browser = "Mozilla/5.0 (Macintosh) Chrome/154"
    rows = [
        (_ts(hours=1), "/", "_all:3", "a", browser, 1280, "https://news.ycombinator.com/x", "US", "pageview"),
        (_ts(days=2), "/", "_all:3", "a", browser, 1280, "", "US", "pageview"),
        (_ts(hours=2), "/sec-analysis", "", "b", browser, 800, "", "GB", "pageview"),
        (_ts(hours=3), "/?utm_source=chatgpt.com", "_all:24", "c", browser, 390, "", "IN", "pageview"),
        (_ts(hours=1), "/", "_all:3", "a", browser, None, "", "US", "feed_expand"),
        (_ts(hours=1), "/", "", "z", "Googlebot/2.1", 0, "", "US", "pageview"),
    ]
    for ts, path, bk, ip, ua, sw, ref, cc, ev in rows:
        if with_event_type:
            con.execute("INSERT INTO pageview_log (ts, path, briefing_key, ip_hash, ua, screen_w, referrer, country, event_type) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (ts, path, bk, ip, ua, sw, ref, cc, ev))
        else:
            con.execute("INSERT INTO pageview_log (ts, path, briefing_key, ip_hash, ua, screen_w, referrer, country) "
                        "VALUES (?,?,?,?,?,?,?,?)", (ts, path, bk, ip, ua, sw, ref, cc))
    con.execute("INSERT INTO briefing_history (cache_key, view_id, hours, generated_at, trigger, sources_json) "
                "VALUES ('_all:3', '_all', 3, ?, 'prewarm', ?)",
                (_ts(hours=1), json.dumps([{"n": 1, "title": "Agency weighs shutdown", "outlet": "Reuters", "chosen": True}])))
    con.execute("INSERT INTO briefing_cache (cache_key, generated_at) VALUES ('_all:3', ?)", (_ts(hours=1),))
    con.commit()
    con.close()


def _full_data_dir(d: Path) -> Path:
    _users_db(d)
    (d / "data_version.txt").write_text(str(int(time.time()) - 1800))
    (d / "pill_judge_health.json").write_text(json.dumps(
        {"status": "ok", "failure_code": None, "recorded_at": datetime.now(timezone.utc).isoformat(),
         "judged": 3, "watermark_after": 99}))
    (d / "logs").mkdir()
    (d / "logs" / "prewarm.log").write_text("[12:00:01] pre-warming 34 curated 3h/24h combos\n[12:05:12] done: 34 combos in 310.7s\n")
    sec = sqlite3.connect(d / "sec.db")
    sec.execute("CREATE TABLE ingest_log (ts TEXT, mode TEXT, ciks_touched INT, rows_written INT, elapsed_s REAL, note TEXT)")
    sec.execute("INSERT INTO ingest_log VALUES (?, 'daily', 33, 737, 30.4, 'ok')",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    sec.commit()
    sec.close()
    from eval.briefing_audit import store
    ev = store.eval_db(d / "briefing_eval.db")
    ev.execute("INSERT INTO runs (kind, started_at, finished_at, briefings, cost_usd) VALUES ('tier1', ?, ?, 1, 0.001)",
               (_ts(minutes=10), _ts(minutes=9)))
    ev.execute("INSERT INTO audits (briefing_id, generated_at, judge_version, view_id, hours, lead_json) VALUES (1, ?, ?, '_all', 3, '{}')",
               (_ts(hours=1), __import__("eval.briefing_audit.judge", fromlist=["x"]).JUDGE_VERSION))
    ev.execute("INSERT INTO sentences (briefing_id, idx, section, text, basis, flags, verdict, confidence, judged, escalate) "
               "VALUES (1, 0, 'summary', 'The agency has shut down the scheme.', '[1]', '[]', 'overstated', 0.9, 1, 1)")
    ev.commit()
    ev.close()
    return d


def test_readers_counts_humans_and_pageviews_only(tmp_path):
    _users_db(tmp_path)
    r = metrics.readers(tmp_path)
    assert r["d30"] == {"views": 4, "visitors": 3}          # bot row and feed_expand excluded
    assert r["d1"]["views"] == 3 and r["returning_30d"] == 1  # "a" seen on two days
    assert r["bot_rows_30d"] == 1
    assert r["feed_expand_7d"]["expands"] == 1 and r["feed_expand_7d"]["home_views"] == 3  # utm "/" is home
    assert dict(r["sources"])["chatgpt.com"] == 1 and dict(r["sources"])["news.ycombinator.com"] == 1


def test_readers_before_event_type_column_uses_screen_w_heuristic(tmp_path):
    _users_db(tmp_path, with_event_type=False)
    r = metrics.readers(tmp_path)
    assert r["d30"]["views"] == 4 and r["feed_expand_7d"]["expands"] == 1


def test_every_section_degrades_instead_of_raising(tmp_path):
    out = metrics.collect(tmp_path, fresh=True)   # empty dir: nothing exists
    for name in ("readers", "quality", "selection", "briefings"):
        assert out[name]["ok"] is False and out[name]["error"]
    assert all(h["status"] == "red" for h in out["health"]["data"])


def test_health_is_rated_from_content(tmp_path):
    d = _full_data_dir(tmp_path)
    rows = {h["job"]: h["status"] for h in metrics.health(d)}
    assert set(rows.values()) == {"green"}, rows
    (d / "data_version.txt").write_text(str(int(time.time()) - 5400))           # 1.5h: amber
    (d / "pill_judge_health.json").write_text(json.dumps(
        {"status": "failed", "failure_code": "credential_missing", "recorded_at": datetime.now(timezone.utc).isoformat()}))
    (d / "logs" / "prewarm.log").write_text("[16:00:01] pre-warming 34 curated 3h/24h combos\n")   # unfinished
    rows = {h["job"]: h for h in metrics.health(d)}
    assert rows["GDELT ingest"]["status"] == "amber"
    assert rows["Pill judge"]["status"] == "red" and "credential_missing" in rows["Pill judge"]["detail"]
    assert rows["Briefing prewarm"]["status"] == "red"


def test_quality_reads_the_audit_db_read_only(tmp_path):
    d = _full_data_dir(tmp_path)
    before = (d / "briefing_eval.db").stat().st_mtime_ns
    q = metrics.quality(d)
    assert q["d1"]["briefings"] == 1 and q["worst"][0]["verdict"] == "overstated"
    assert q["worst"][0]["sources"] == ["Reuters: Agency weighs shutdown"]
    assert (d / "briefing_eval.db").stat().st_mtime_ns == before


def test_spend_dedupes_by_alias_and_never_exposes_keys(tmp_path, monkeypatch):
    (tmp_path / ".openrouter_key").write_text("sk-secret-one")
    (tmp_path / ".gdelt_pill_judge_gateway_key").write_text("sk-secret-one")
    (tmp_path / ".gdelt_eval_gateway_key").write_text("sk-secret-two")
    info = {"sk-secret-one": {"key_alias": "gdelt", "spend": 9.75, "max_budget": 30.0, "budget_duration": "30d"},
            "sk-secret-two": {"key_alias": "gdelt-eval", "spend": 0.21, "max_budget": 3.0, "budget_duration": "30d"}}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        key = req.headers["Authorization"].split()[-1]
        return Resp(json.dumps({"info": info[key]}).encode())

    monkeypatch.setattr(metrics.urllib.request, "urlopen", fake_urlopen)
    s = metrics.spend(tmp_path)
    assert [r["alias"] for r in s["rows"]] == ["gdelt", "gdelt-eval"]
    assert s["rows"][0]["used_by"] == ["briefings (legacy key)", "pill judge"] and s["rows"][0]["pct"] == 32
    assert "sk-secret" not in json.dumps(s)


# ── route: admin only ────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from flask_login import LoginManager, UserMixin
    import routes.admin_metrics as am

    class U(UserMixin):
        def __init__(self, uid, admin):
            self.id, self.is_admin = uid, admin

    users = {"1": U("1", True), "2": U("2", False)}
    app = flask.Flask(__name__, template_folder=str(ROOT / "dashboard" / "templates"))
    app.secret_key = "test"
    lm = LoginManager(app)
    lm.login_view = "login"
    lm.user_loader(users.get)
    pages = flask.Blueprint("pages", __name__)
    pages.add_url_rule("/", "index", lambda: "home")
    app.register_blueprint(pages)
    app.add_url_rule("/login", "login", lambda: "login")
    app.register_blueprint(am.bp)
    d = _full_data_dir(tmp_path)
    real_collect = metrics.collect
    monkeypatch.setattr(am.metrics, "collect", lambda fresh=False: real_collect(d, fresh=True))
    return app.test_client()


def _login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = uid
        s["_fresh"] = True


def test_anonymous_is_sent_to_login(client):
    r = client.get("/admin/metrics")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_non_admin_is_redirected_and_json_forbidden(client):
    _login(client, "2")
    assert client.get("/admin/metrics").status_code == 302
    assert client.get("/admin/metrics.json").status_code == 403


def test_admin_sees_every_section(client):
    _login(client, "1")
    r = client.get("/admin/metrics")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    for heading in ("Pipeline health", "Readers", "Briefing accuracy", "Story selection", "LLM spend", "Briefing generation"):
        assert heading in html
    assert "The agency has shut down the scheme." in html
    j = client.get("/admin/metrics.json").get_json()
    assert j["readers"]["data"]["d30"]["views"] == 4


# ── pageview endpoint: feed_expand is an event, not a pageview ──────────────

def test_pageview_endpoint_labels_events_and_backfills_once(tmp_path, monkeypatch):
    import models
    import routes.api_briefing as api_briefing
    db = tmp_path / "users.db"
    _users_db(tmp_path, with_event_type=False)
    before = sqlite3.connect(db).execute("SELECT count(*) FROM pageview_log").fetchone()[0]
    monkeypatch.setattr(models, "get_user_db", lambda: sqlite3.connect(db))
    app = flask.Flask(__name__)
    app.register_blueprint(api_briefing.bp)
    c = app.test_client()
    c.post("/api/pageview", json={"path": "/", "screen_w": 1280})
    c.post("/api/pageview", json={"path": "/", "event_type": "feed_expand"})
    c.post("/api/pageview", json={"path": "/", "event_type": "<script>", "screen_w": 1})
    con = sqlite3.connect(db)
    types = [r[0] for r in con.execute("SELECT event_type FROM pageview_log ORDER BY id")]
    assert len(types) == before + 3
    # the old no-screen_w row was relabelled once; new rows carry their own type
    assert types.count("feed_expand") == 2
    assert types[-3:] == ["pageview", "feed_expand", "pageview"]
