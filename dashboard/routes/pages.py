"""HTML page routes: index, service worker, search, event detail, about."""

import json
from datetime import datetime

from flask import Blueprint, render_template, current_app

from db import get_db

bp = Blueprint("pages", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


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
