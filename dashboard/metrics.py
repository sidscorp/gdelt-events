"""Read-only collectors for /admin/metrics.

Every section returns {"ok": bool, "data": ..., "error": str | None}: one broken
source renders as an error card, never as a 500. Every SQLite file is opened
mode=ro; nothing here writes. Health rows are rated from what the job actually
produced (timestamps, statuses, notes), never from exit codes.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from _paths import DATA_DIR

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:           # eval/ and pipeline/ live at the repo root
    sys.path.insert(0, str(_REPO))

GATEWAY_KEY_INFO = "https://llm.snambiar.com/key/info"
BOT_RE = re.compile(r"bot|crawl|spider|slurp|headless|preview|facebookexternalhit|curl|python-requests|wget", re.I)
PROBLEM = ("overstated", "unsupported", "contradicted")
KEY_FILES = {  # file in data/ -> what uses it (a key alias can serve several)
    ".openrouter_key": "briefings (legacy key)",
    ".gdelt_briefings_gateway_key": "briefings",
    ".gdelt_pill_judge_gateway_key": "pill judge",
    ".gdelt_eval_gateway_key": "briefing eval",
    ".social_gateway_key": "social signals",
}
CACHE_S = 60
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _section(fn, *args) -> dict:
    try:
        return {"ok": True, "data": fn(*args), "error": None}
    except Exception as e:  # a metrics page must degrade per section
        return {"ok": False, "data": None, "error": f"{type(e).__name__}: {e}"[:300]}


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"{path.name} not found")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            d = datetime.strptime(s.replace("+00:00", "+0000"), fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _age_s(when: datetime | None) -> float | None:
    return None if when is None else (_now() - when).total_seconds()


# ── readers ──────────────────────────────────────────────────────────────────

def _page_type(path: str) -> str:
    p = (path or "/").split("?")[0]
    if p in ("", "/"):
        return "home"
    for prefix, name in (("/event", "event pages"), ("/sec-analysis", "SEC financials"),
                         ("/about", "about/methodology"), ("/methodology", "about/methodology"),
                         ("/portal", "portal"), ("/admin", "admin")):
        if p.startswith(prefix):
            return name
    return "other"


def _source(referrer: str, path: str) -> str:
    utm = parse_qs(urlparse(path or "").query).get("utm_source")
    if utm:
        return utm[0][:40]
    host = urlparse(referrer or "").netloc.lower().removeprefix("www.")
    if not host:
        return "(direct)"
    return "(internal)" if host.endswith("gdeltmonitor.com") else host


def readers(data_dir: Path = DATA_DIR) -> dict:
    con = _ro(data_dir / "users.db")
    cols = {r[1] for r in con.execute("PRAGMA table_info(pageview_log)")}
    # Before the event_type column exists, feed_expand beacons are the rows with
    # no screen_w since 09-19 (see api_pageview).
    etype = ("coalesce(event_type, 'pageview')" if "event_type" in cols else
             "CASE WHEN screen_w IS NULL AND ts >= '2026-09-19' THEN 'feed_expand' ELSE 'pageview' END")
    since = (_now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute(f"SELECT ts, path, briefing_key, ip_hash, ua, referrer, country, {etype} AS et "
                       "FROM pageview_log WHERE ts >= ?", (since,)).fetchall()
    con.close()
    bots = [r for r in rows if BOT_RE.search(r["ua"] or "")]
    human = [r for r in rows if not BOT_RE.search(r["ua"] or "")]
    views = [r for r in human if r["et"] == "pageview"]
    expands = [r for r in human if r["et"] == "feed_expand"]
    now = _now()

    def window(days):
        cut = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        w = [r for r in views if r["ts"] >= cut]
        return {"views": len(w), "visitors": len({r["ip_hash"] for r in w})}

    days_by_ip = defaultdict(set)
    for r in views:
        days_by_ip[r["ip_hash"]].add(r["ts"][:10])
    daily = defaultdict(lambda: [0, set()])
    for r in views:
        daily[r["ts"][:10]][0] += 1
        daily[r["ts"][:10]][1].add(r["ip_hash"])
    series = []
    for i in range(29, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        series.append({"day": d, "views": daily[d][0], "visitors": len(daily[d][1])})
    home7 = [r for r in views if _page_type(r["path"]) == "home"
             and r["ts"] >= (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")]
    exp7 = [r for r in expands if r["ts"] >= (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")]
    return {
        "d1": window(1), "d7": window(7), "d30": window(30),
        "returning_30d": sum(1 for s in days_by_ip.values() if len(s) >= 2),
        "series": series,
        "top_briefings": Counter(r["briefing_key"] or "(none)" for r in views).most_common(8),
        "page_types": Counter(_page_type(r["path"]) for r in views).most_common(),
        "sources": Counter(_source(r["referrer"], r["path"]) for r in views).most_common(8),
        "countries": Counter(r["country"] or "?" for r in views).most_common(8),
        "feed_expand_7d": {"expands": len(exp7), "home_views": len(home7),
                           "rate": round(len(exp7) / len(home7), 3) if home7 else None},
        "bot_rows_30d": len(bots),
    }


# ── briefings ────────────────────────────────────────────────────────────────

def briefings(data_dir: Path = DATA_DIR) -> dict:
    con = _ro(data_dir / "users.db")
    since = (_now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
    per_day = defaultdict(Counter)
    for r in con.execute("SELECT substr(generated_at,1,10) d, trigger, count(*) n FROM briefing_history "
                         "WHERE generated_at >= ? GROUP BY 1, 2", (since,)):
        per_day[r["d"]][r["trigger"] or "?"] += r["n"]
    cache = {r["cache_key"]: r["generated_at"] for r in con.execute("SELECT cache_key, generated_at FROM briefing_cache")}
    con.close()
    try:
        from pipeline.prewarm_briefings import curated_combos
        combos = curated_combos()
    except Exception:
        combos = [("", 3), ("", 24)]
    try:
        from briefing import fresh_s
    except Exception:
        def fresh_s(hours):
            return {3: 3 * 3600, 24: 8 * 3600}.get(int(hours), 6 * 3600)
    fresh = stale = missing = 0
    stalest = []
    for view, hours in combos:
        key = f"{view or '_all'}:{hours}"
        age = _age_s(_parse_ts(cache.get(key)))
        if age is None:
            missing += 1
        elif age <= fresh_s(hours):
            fresh += 1
        else:
            stale += 1
            stalest.append((key, round(age / 3600, 1)))
    days = sorted(per_day)
    return {
        "per_day": [{"day": d, "total": sum(per_day[d].values()), **per_day[d]} for d in days],
        "avg_per_day_7d": round(sum(sum(per_day[d].values()) for d in days[-7:]) / max(1, len(days[-7:])), 1),
        "curated": {"keys": len(combos), "fresh": fresh, "stale": stale, "missing": missing,
                    "stalest": sorted(stalest, key=lambda x: -x[1])[:5]},
    }


# ── briefing quality (Tier 1 audit) ──────────────────────────────────────────

def quality(data_dir: Path = DATA_DIR) -> dict:
    from eval.briefing_audit import store
    from eval.briefing_audit.judge import JUDGE_VERSION
    from eval.briefing_audit.parse import parse_sources
    from eval.briefing_audit.run import report
    ev = store.eval_db_ro(data_dir / "briefing_eval.db")
    out = {"judge_version": JUDGE_VERSION, "d1": report(1, ev), "d7": report(7, ev)}
    worst = ev.execute(
        "SELECT s.briefing_id, s.section, s.text, s.verdict, s.confidence, s.basis, a.generated_at, a.view_id, a.hours "
        "FROM sentences s JOIN audits a USING (briefing_id) "
        f"WHERE a.judge_version = ? AND s.verdict IN ({','.join('?' * len(PROBLEM))}) "
        "AND s.section IN ('summary', 'highlight') ORDER BY a.generated_at DESC, s.idx LIMIT 5",
        (JUDGE_VERSION, *PROBLEM)).fetchall()
    ev.close()
    users = _ro(data_dir / "users.db")
    items = []
    for w in worst:
        b = users.execute("SELECT sources_json FROM briefing_history WHERE id = ?", (w["briefing_id"],)).fetchone()
        srcs = parse_sources(b["sources_json"], None) if b else {}
        items.append({**{k: w[k] for k in ("briefing_id", "section", "text", "verdict", "generated_at", "view_id", "hours")},
                      "confidence": round(w["confidence"] or 0, 2),
                      "sources": [f"{srcs[n].outlet}: {srcs[n].title}" for n in json.loads(w["basis"] or "[]") if n in srcs][:3]})
    users.close()
    out["worst"] = items
    replays = sorted((data_dir / "replay").glob("*.jsonl")) if (data_dir / "replay").exists() else []
    if replays:
        from eval.briefing_audit.replay import summarize
        recs = [json.loads(l) for l in replays[-1].open(encoding="utf-8") if l.strip()]
        s = summarize([r for r in recs if "error" not in r])
        out["replay"] = {"file": replays[-1].name, "pairs": len(recs),
                         "A": s["A"]["problem_rate"], "B": s["B"]["problem_rate"],
                         "change": s["problem_rate_change_B_minus_A"]}
    return out


def selection(data_dir: Path = DATA_DIR) -> dict:
    from eval.briefing_audit.selection import audit
    return audit(7, users=_ro(data_dir / "users.db"))


# ── spend (gateway /key/info, one call per key; keys are never rendered) ────

def spend(data_dir: Path = DATA_DIR, timeout: float = 5) -> dict:
    by_alias: dict[str, dict] = {}
    errors = []
    for fname, purpose in KEY_FILES.items():
        path = data_dir / fname
        if not path.exists():
            continue
        req = urllib.request.Request(GATEWAY_KEY_INFO, headers={"Authorization": f"Bearer {path.read_text().strip()}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                info = json.loads(r.read().decode("utf-8", "replace")).get("info") or {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            errors.append(f"{purpose}: {type(e).__name__}")
            continue
        alias = info.get("key_alias") or "(unnamed key)"
        row = by_alias.setdefault(alias, {
            "alias": alias, "used_by": [], "spend": round(float(info.get("spend") or 0), 2),
            "cap": info.get("max_budget"), "period": info.get("budget_duration"),
            "resets": (info.get("budget_reset_at") or "")[:10]})
        row["used_by"].append(purpose)
    for row in by_alias.values():
        row["pct"] = round(100 * row["spend"] / row["cap"]) if row["cap"] else None
    if not by_alias and errors:
        raise RuntimeError("; ".join(errors))
    # "rows", not "keys": in Jinja, data.keys would resolve to dict.keys().
    return {"rows": sorted(by_alias.values(), key=lambda r: -r["spend"]), "errors": errors}


# ── pipeline health ──────────────────────────────────────────────────────────

def _rate(age: float | None, green_s: float, problem: str | None = None) -> str:
    if problem:
        return "red"
    if age is None:
        return "red"
    return "green" if age <= green_s else ("amber" if age <= 2 * green_s else "red")


def _fmt_age(age: float | None) -> str:
    if age is None:
        return "never"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 172800:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"


def health(data_dir: Path = DATA_DIR) -> list[dict]:
    rows = []

    def add(job, fn):
        try:
            age, detail, problem, green_s = fn()
            rows.append({"job": job, "status": _rate(age, green_s, problem), "age": _fmt_age(age),
                         "detail": problem or detail})
        except Exception as e:
            rows.append({"job": job, "status": "red", "age": "?", "detail": f"{type(e).__name__}: {e}"[:160]})

    def ingest():
        ver = int((data_dir / "data_version.txt").read_text().strip())
        return time.time() - ver, "GDELT data_version", None, 3600

    def pill_judge():
        h = json.loads((data_dir / "pill_judge_health.json").read_text())
        age = _age_s(_parse_ts(h.get("recorded_at")))
        problem = None if h.get("status") == "ok" and not h.get("failure_code") else \
            f"status={h.get('status')} failure={h.get('failure_code')}"
        return age, f"judged {h.get('judged', 0)} · watermark {h.get('watermark_after')}", problem, 3600

    def prewarm():
        log = data_dir / "logs" / "prewarm.log"
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
        done = [i for i, l in enumerate(tail) if " done: " in l]
        started = [i for i, l in enumerate(tail) if "pre-warming" in l]
        problem = None if done and (not started or done[-1] > started[-1]) else "last run did not finish"
        # The log carries only HH:MM:SS, so the file's mtime dates the last write.
        age = time.time() - log.stat().st_mtime
        return age, (tail[done[-1]].split("] ", 1)[-1] if done else ""), problem, 5 * 3600

    def sec():
        con = _ro(data_dir / "sec.db")
        r = con.execute("SELECT ts, ciks_touched, rows_written, note FROM ingest_log WHERE mode = 'daily' "
                        "ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        if not r:
            return None, "", "no daily run recorded", 30 * 3600
        problem = None if (r["note"] or "").lower() == "ok" else f"note={r['note']}"
        return _age_s(_parse_ts(r["ts"])), f"{r['ciks_touched']} filers · {r['rows_written']} rows", problem, 30 * 3600

    def tier1():
        con = _ro(data_dir / "briefing_eval.db")
        r = con.execute("SELECT started_at, finished_at, briefings, cost_usd, note FROM runs WHERE kind = 'tier1' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        if not r:
            return None, "", "never run (task not scheduled?)", 3600
        problem = r["note"] if r["note"] else (None if r["finished_at"] else "run did not finish")
        return _age_s(_parse_ts(r["started_at"])), f"{r['briefings']} briefings · ${r['cost_usd'] or 0:.4f}", problem, 3600

    for job, fn in (("GDELT ingest", ingest), ("Pill judge", pill_judge), ("Briefing prewarm", prewarm),
                    ("SEC ingest", sec), ("Briefing audit (Tier 1)", tier1)):
        add(job, fn)
    return rows


# ── entry point ──────────────────────────────────────────────────────────────

def collect(data_dir: Path = DATA_DIR, fresh: bool = False) -> dict:
    key = str(data_dir)
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and time.time() - hit[0] < CACHE_S:
            return hit[1]
    t0 = time.time()
    out = {
        "generated_at": _now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "health": _section(health, data_dir),
        "readers": _section(readers, data_dir),
        "quality": _section(quality, data_dir),
        "selection": _section(selection, data_dir),
        "spend": _section(spend, data_dir),
        "briefings": _section(briefings, data_dir),
    }
    out["elapsed_s"] = round(time.time() - t0, 2)
    with _lock:
        _cache[key] = (time.time(), out)
    return out
