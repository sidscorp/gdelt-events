"""HTML page routes: index (SSR first paint), sitemap/robots, service worker,
search, event detail, about."""

import html
import json
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


def _briefing_html(view_id, hours):
    """Cached AI briefing rendered to minimal safe HTML. Read-only — never
    generates. The client re-renders (and refreshes) it via markdown.js."""
    from models import get_user_db
    cache_key = f"{view_id or '_all'}:{hours}"
    try:
        con = get_user_db()
        row = con.execute(
            "SELECT briefing, generated_at FROM briefing_cache WHERE cache_key = ?",
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

    out, in_list = [], False
    for line in row[0].splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith(("- ", "* ", "• ")):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{html.escape(line[2:].strip())}</li>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        out.append("</ul>")
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
        feed_mod = datetime.utcfromtimestamp(int(_data_version())).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00")
    except Exception:
        feed_mod = None

    entries = [(f"{CANON_BASE}/", feed_mod, "hourly", "1.0")]
    for v in VIEWS:
        entries.append((f"{CANON_BASE}/?view={v['id']}", feed_mod, "hourly", "0.8"))
    for path in ("/about", "/methodology", "/search"):
        entries.append((f"{CANON_BASE}{path}", None, "monthly", "0.3"))

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


@bp.route("/about")
def about():
    return render_template("about.html")


@bp.route("/methodology")
def methodology():
    """Transparency: the logic behind everything the dashboard displays."""
    return render_template("methodology.html")
