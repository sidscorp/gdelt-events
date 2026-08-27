"""HTML page routes: index (SSR first paint), sitemap/robots, service worker,
search, event detail, about."""

import html
import json
import re
import time
from datetime import datetime

from flask import Blueprint, render_template, current_app, request, Response

from db import get_db
from views import VIEWS, find_view

bp = Blueprint("pages", __name__)

CANON_BASE = "https://gdeltmonitor.com"

SITE_TITLE = "GDELT Monitor"
SITE_DESC = (
    "Real-time monitoring of 44,000 global news sources, updated every 15 "
    "minutes. Curated topic feeds, AI briefings, event clustering and "
    "semantic search over world news."
)

_VALID_HOURS = {3, 6, 24, 72, 168, 720}


def _resolve_view_hours():
    """(view dict | None, hours int) from the request URL, defaulted like the
    client: curated view's default_hours, else 3h."""
    view_id = (request.args.get("view") or "").strip()
    view = find_view(view_id) if view_id else None
    try:
        hours = int(request.args.get("hours", 0))
    except (TypeError, ValueError):
        hours = 0
    if hours not in _VALID_HOURS:
        hours = (view.get("default_hours") if view else None) or 3
    return view, hours


def _ssr_feed(view_id, hours):
    """Page 1 of the feed straight from the warmed in-process cache.

    Cache hit or nothing: this path must NEVER open a DuckDB connection or
    trigger a recompute (issue #6). The probe request context mirrors the
    exact query string the client and pipeline/warm_feed.py send, so the key
    lands on the warmed entry.
    """
    from articles import _feed_cache, _feed_cache_key
    qs = f"hours={hours}&match_types=legal&en_only=1&page=1&per_page=50"
    if view_id:
        qs += f"&view={view_id}"
    try:
        with current_app.test_request_context(f"/api/articles?{qs}"):
            key = _feed_cache_key()
        hit = _feed_cache.get(key)
        if not hit or hit[0] <= time.time():
            return None
        data = json.loads(hit[1])
        return data if data.get("articles") else None
    except Exception:
        return None


_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITAL = re.compile(r"\*([^*\n]+)\*")
_MD_LI = re.compile(r"^(?:[-*•‣▪–]|\d+[.)])\s+(.*)$")
_MD_H = re.compile(r"^#{1,4}\s+(.*)$")
_MD_CITE = re.compile(r"\[(\d+)\]")


def _md_inline(s, sources):
    """Inline markdown + [N] citations. Mirrors _mdInline/linkifyCitations in
    static/js/markdown.js — the SSR paint and the client re-render must be the
    same HTML, or the briefing visibly reflows a beat after load."""
    s = html.escape(s, quote=False)
    s = _MD_BOLD.sub(r"<strong>\1</strong>", s)
    s = _MD_ITAL.sub(r"<em>\1</em>", s)

    def cite(m):
        idx = int(m.group(1)) - 1
        src = sources[idx] if 0 <= idx < len(sources) else None
        if not src or not src.get("link"):
            return m.group(0)
        cnt = f" · {src['n_sources']} sources" if (src.get("n_sources") or 0) > 1 else ""
        tip = html.escape(
            (src.get("outlet") or "source") + cnt
            + (f" — {src['title']}" if src.get("title") else ""),
            quote=True,
        )
        link = html.escape(src["link"], quote=True)
        return (f'<sup class="cite"><a href="{link}" target="_blank" '
                f'rel="noopener" title="{tip}">{m.group(1)}</a></sup>')

    return _MD_CITE.sub(cite, s)


def _briefing_html(view_id, hours):
    """Cached AI briefing rendered to the same HTML markdown.js produces, so
    the server's first paint and the client's re-render are identical. Read-only
    — never generates. The client still refreshes it via markdown.js."""
    from models import get_user_db
    from briefing import _normalize_text
    cache_key = f"{view_id or '_all'}:{hours}"
    try:
        con = get_user_db()
        row = con.execute(
            "SELECT briefing, generated_at, sources_json FROM briefing_cache "
            "WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        con.close()
        if not row or not row[0]:
            return None
        age_s = (datetime.utcnow()
                 - datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")).total_seconds()
        if age_s > 48 * 3600:
            return None
    except Exception:
        return None

    try:
        sources = json.loads(row[2]) if row[2] else []
    except Exception:
        sources = []

    # Rows cached before the normalizer learned about 【N】 citations and
    # non-breaking punctuation still hold the raw model text — clean on read.
    out, items, para = [], [], []

    def flush_para():
        if para:
            out.append(f"<p>{_md_inline(' '.join(para), sources)}</p>")
            para.clear()

    def flush_list():
        if items:
            out.append("<ul>" + "".join(items) + "</ul>")
            items.clear()

    for line in _normalize_text(row[0]).splitlines():
        line = line.strip()
        if not line:
            flush_para(); flush_list()
            continue
        m = _MD_LI.match(line)
        if m:
            flush_para()
            items.append(f"<li>{_md_inline(m.group(1), sources)}</li>")
            continue
        m = _MD_H.match(line)
        if m:
            flush_para(); flush_list()
            out.append(f"<h4>{_md_inline(m.group(1), sources)}</h4>")
            continue
        flush_list()
        para.append(line)
    flush_para(); flush_list()
    return "".join(out) if out else None


@bp.route("/")
def index():
    view, hours = _resolve_view_hours()
    view_id = view["id"] if view else ""

    # SSR only for the clean shareable shapes the cache is warmed for:
    # curated views / global, English, page 1. Anything else (custom pills,
    # en_only=0, extra filters) falls back to the client-rendered shell.
    ssr_ok = (
        (view is not None or not request.args.get("view"))
        and request.args.get("en_only") != "0"
        and not any(request.args.get(k) for k in (
            "q", "person", "org", "location", "theme", "domain", "outlet",
            "date_from", "date_to", "page"))
    )
    feed = _ssr_feed(view_id, hours) if ssr_ok else None
    briefing = _briefing_html(view_id, hours) if ssr_ok else None

    if view:
        page_title = f"{view['name']} — {SITE_TITLE}"
        meta_desc = f"{view['description']} Live coverage from 44K global news sources."
        canonical = f"{CANON_BASE}/?view={view['id']}"
    else:
        page_title = f"{SITE_TITLE} — Real-time global news monitoring"
        meta_desc = SITE_DESC
        canonical = f"{CANON_BASE}/"

    return render_template(
        "index.html",
        page_title=page_title,
        meta_desc=meta_desc,
        canonical=canonical,
        og_image=f"{CANON_BASE}/static/og-card.png",
        ssr_articles=(feed or {}).get("articles"),
        ssr_total=(feed or {}).get("total"),
        ssr_snap_key=f"snap:{view_id}|{hours}|1|importance" if feed else None,
        ssr_briefing=briefing,
        ssr_briefing_key=f"{view_id}|{hours}",
        ssr_view=view_id,
        ssr_hours=hours,
    )


# --- sitemap + robots --------------------------------------------------------

_sitemap_cache = {"at": 0.0, "xml": None}
_SITEMAP_TTL_S = 3600


def _w3c(ts):
    """YYYYMMDDHHMMSS bigint -> W3C datetime, or None."""
    if not ts:
        return None
    s = str(int(ts)).zfill(14)
    try:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:00+00:00"
    except Exception:
        return None


@bp.route("/sitemap.xml")
def sitemap():
    now = time.time()
    if _sitemap_cache["xml"] and now - _sitemap_cache["at"] < _SITEMAP_TTL_S:
        return Response(_sitemap_cache["xml"], mimetype="application/xml")

    from articles import _data_version
    try:
        version = int(_data_version())
        feed_mod = (datetime.utcfromtimestamp(version).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00") if version > 0 else None)
    except Exception:
        feed_mod = None

    entries = [(f"{CANON_BASE}/", feed_mod, "hourly", "1.0")]
    for v in VIEWS:
        entries.append((f"{CANON_BASE}/?view={v['id']}", feed_mod, "hourly", "0.8"))
    for path in ("/about", "/methodology", "/search", "/sec-analysis"):
        entries.append((f"{CANON_BASE}{path}", None, "monthly", "0.3"))

    # /sec-analysis is a search form until it is given a ticker, so the bare URL
    # shows a crawler nothing. Seed the handful the page itself suggests; the store
    # holds 17,934 filers and listing them would be sitemap spam, not coverage.
    from routes.sec_analysis import SUGGESTED
    for tick, _name in SUGGESTED:
        entries.append((f"{CANON_BASE}/sec-analysis?ticker={tick}", None, "weekly", "0.4"))

    # Recent event permalinks: substantial (size>=3), newest first, capped.
    con = get_db()
    if con is not None:
        try:
            rows = con.execute(
                "SELECT cluster_id, latest_seen FROM clusters "
                "WHERE size >= 3 ORDER BY latest_seen DESC LIMIT 500"
            ).fetchall()
            for cid, latest in rows:
                entries.append((f"{CANON_BASE}/event/{cid}", _w3c(latest), None, "0.5"))
        except Exception:
            pass
        finally:
            try: con.close()
            except Exception: pass

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod, changefreq, priority in entries:
        e = [f"<loc>{html.escape(loc)}</loc>"]
        if lastmod:
            e.append(f"<lastmod>{lastmod}</lastmod>")
        if changefreq:
            e.append(f"<changefreq>{changefreq}</changefreq>")
        if priority:
            e.append(f"<priority>{priority}</priority>")
        parts.append("<url>" + "".join(e) + "</url>")
    parts.append("</urlset>")
    xml = "\n".join(parts)

    _sitemap_cache.update(at=now, xml=xml)
    return Response(xml, mimetype="application/xml")


@bp.route("/robots.txt")
def robots():
    return Response(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /login\n"
        "Disallow: /api/\n"
        f"Sitemap: {CANON_BASE}/sitemap.xml\n",
        mimetype="text/plain",
    )


@bp.route("/sw.js")
def service_worker():
    """Serve the service worker from root scope so it controls the whole site."""
    from flask import send_from_directory
    resp = send_from_directory(current_app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"  # so SW updates propagate immediately
    return resp


@bp.route("/search")
def search_page():
    """Standalone semantic search interface."""
    return render_template("search.html")


def _fmt_event_ts(ts):
    """Format a YYYYMMDDHHMMSS bigint as a readable UTC string."""
    if not ts:
        return ""
    s = str(int(ts)).zfill(14)
    try:
        return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                        int(s[8:10]), int(s[10:12])).strftime("%b %d, %Y %H:%M UTC")
    except Exception:
        return ""


@bp.route("/event/<cluster_id>")
def event_detail(cluster_id):
    """Persistent, shareable event page: representative + all variants.

    Renders from the denormalized members_json snapshot so the page survives
    even after the underlying articles age out of the embedding window.
    """
    con = get_db()
    row = None
    if con is not None:
        try:
            row = con.execute(
                "SELECT cluster_id, rep_url, title, image, size, first_seen, latest_seen, members_json "
                "FROM clusters WHERE cluster_id = ?",
                [cluster_id],
            ).fetchone()
        except Exception:
            row = None
        finally:
            con.close()
    if not row:
        return render_template("event_detail.html", cluster=None,
                               error="Event not found"), 404

    cid, rep_url, title, image, size, first_seen, latest_seen, mjson = row
    members = json.loads(mjson) if mjson else []
    members.sort(key=lambda m: m.get("crawled_at") or 0, reverse=True)
    for m in members:
        m["when"] = _fmt_event_ts(m.get("crawled_at"))
    cluster = {
        "id": cid,
        "rep_url": rep_url,
        "title": title or "(untitled event)",
        "image": image,
        "size": size,
        "first_seen": _fmt_event_ts(first_seen),
        "latest_seen": _fmt_event_ts(latest_seen),
        "members": members,
    }
    return render_template("event_detail.html", cluster=cluster, error=None)



# ── documentation facts ──────────────────────────────────────────────────────
# /about and /methodology make concrete claims — model names, event limits, cache
# windows, scoring weights, how many pills exist. Every one of those was hand-copied
# prose duplicating a constant in code, and on 2026-08-02 six of them were found
# wrong across three pages at once (GLM-4.7, Gemini 2.5 Flash, OpenRouter, "top 50
# events", "45 minutes", "a few built-in views" when there were 16). Inject them from
# the source of truth instead, so the pages cannot drift again.
def _doc_facts():
    facts = {}
    try:
        from briefing import (BRIEFING_MODEL, BRIEFING_EVENT_LIMIT,
                              BRIEFING_CANDIDATE_LIMIT, fresh_s)
        facts["model"] = BRIEFING_MODEL.rsplit("/", 1)[-1]
        facts["event_limit"] = BRIEFING_EVENT_LIMIT
        facts["candidate_limit"] = BRIEFING_CANDIDATE_LIMIT
        facts["fresh_short_h"] = fresh_s(3) // 3600
        facts["fresh_long_h"] = fresh_s(720) // 3600
    except Exception:
        pass
    try:
        from importance import IMP_W_COVERAGE, IMP_W_VELOCITY, IMP_W_RECENCY
        facts["w_coverage"] = IMP_W_COVERAGE
        facts["w_velocity"] = IMP_W_VELOCITY
        facts["w_recency"] = IMP_W_RECENCY
    except Exception:
        pass
    try:
        facts["n_views"] = len(VIEWS)
        by_group = {}
        for v in VIEWS:
            by_group.setdefault(v.get("group", "Other"), []).append(v)
        facts["view_groups"] = by_group
    except Exception:
        pass
    # The judge is a DIFFERENT model from the briefing writer. The page used to
    # render BRIEFING_MODEL for both; that was only correct by coincidence and
    # would have gone quietly wrong the first time either was repointed.
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _repo = str(_Path(__file__).resolve().parent.parent.parent)
        if _repo not in _sys.path:
            _sys.path.insert(0, _repo)
        from pipeline.pill_eval import JUDGE_MODEL
        facts["judge_model"] = JUDGE_MODEL.rsplit("/", 1)[-1]
    except Exception:
        pass
    facts.update(_pill_precision_facts())
    return facts


# Precision claims on /methodology are the ones most likely to rot, because they
# are measurements rather than constants — and stale ones are worse than none:
# the "75-94%" figure survived six weeks during which the judge was not running
# at all. Read them from the newest pill_eval report so the page states what was
# last actually measured, and degrades to the template defaults if none exists.
def _pill_precision_facts():
    out = {}
    try:
        import glob
        import json as _json
        from _paths import DATA_DIR
        reports = sorted(glob.glob(str(DATA_DIR / "pill_eval" / "*.json")))
        if not reports:
            return out
        data = _json.loads(open(reports[-1], encoding="utf-8").read())
        scored = {r["category"]: r["precision"] for r in data.get("results", [])
                  if r.get("precision") is not None
                  # 'fda' samples the raw FDA name-match cache, which no longer
                  # backs any live pill — including it would report a 10% floor
                  # for something no reader can open.
                  and r["category"] != "fda"}
        if not scored:
            return out
        vals = sorted(scored.values())
        mid = len(vals) // 2
        median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        pct = lambda v: f"{round(v * 100)}%"
        out["eval_min"], out["eval_max"] = pct(vals[0]), pct(vals[-1])
        out["eval_median"] = pct(median)
        out["eval_pills"] = len(vals)
        out["eval_n"] = data.get("n")
        ran = (data.get("ran_at") or "")[:10]
        if ran:
            # Build the day number by hand: '%-d' is glibc-only and this runs on
            # Windows, where it raises.
            d = datetime.strptime(ran, "%Y-%m-%d")
            out["eval_date"] = f"{d.day} {d.strftime('%B %Y')}"
        for key, cat in (("eval_supply", "supply_chain"), ("eval_semi", "semiconductors"),
                         ("eval_aireg", "ai_regulation"), ("eval_cyber", "cyber_attacks")):
            if cat in scored:
                out[key] = pct(scored[cat])
    except Exception:
        pass
    return out


@bp.route("/about")
def about():
    return render_template("about.html", **_doc_facts())


@bp.route("/methodology")
def methodology():
    """Transparency: the logic behind everything the dashboard displays."""
    return render_template("methodology.html", **_doc_facts())
