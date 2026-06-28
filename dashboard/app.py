"""GDELT News Dashboard — Tufte-inspired breaking news viewer."""

import json
import logging
import logging.handlers
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
from flask import (
    Flask, render_template, request, jsonify, g,
    redirect, url_for, flash,
)
from flask_login import login_user, logout_user, login_required, current_user

from views import VIEWS, find_view
from models import (
    init_user_db, create_user, authenticate, email_exists,
    get_user_pills, create_pill, create_semantic_pill,
    get_pill, get_pill_job_status,
    delete_pill, list_users, approve_user, reject_user,
)
from auth import init_auth, User
from db import get_db, _arm_statement_timeout, _hours_cutoff
from parsers import (
    parse_tone, parse_enhanced_list, parse_locations,
    extract_title, format_timestamp, time_ago,
)
from briefing import (
    BRIEFING_TTL_S, BRIEFING_EVENT_LIMIT,
    _fetch_briefing_events, _generate_briefing, _generate_briefing_stream,
)

from articles import (
    _api_articles_inner, _feed_cache, _feed_cache_key,
    _FEED_TTL_S, _FEED_CACHE_MAX, _parse_date_filters, GAL_COLS,
)

from _paths import DB_PATH, LOG_DIR, OPENROUTER_KEY_PATH
LOG_DIR.mkdir(parents=True, exist_ok=True)
(DB_PATH.parent / "duckdb_tmp").mkdir(parents=True, exist_ok=True)


app = Flask(__name__)
app.jinja_env.filters["from_json"] = json.loads
init_user_db()
init_auth(app)

# Dedicated request logger — captures per-request timing so backend slowness
# is visible without guessing. Rotating file handler caps disk usage.
req_log = logging.getLogger("dashboard.requests")
if not req_log.handlers:
    req_log.setLevel(logging.INFO)
    _h = logging.handlers.RotatingFileHandler(
        LOG_DIR / "dashboard.log", maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    req_log.addHandler(_h)
    req_log.propagate = False

@app.before_request
def _req_start():
    g._req_t0 = time.perf_counter()
    g._req_phases = {}


@app.after_request
def _req_end(resp):
    # Always revalidate HTML so deployed UI changes show on a normal reload
    # (no hard-refresh needed). The page is tiny + gzipped, so this is cheap.
    if resp.headers.get("Content-Type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    t0 = getattr(g, "_req_t0", None)
    if t0 is None:
        return resp
    dt = time.perf_counter() - t0
    phases = getattr(g, "_req_phases", {}) or {}
    phase_s = " ".join(f"{k}={int(v*1000)}ms" for k, v in phases.items())
    qs = request.query_string.decode("ascii", "replace")[:120]
    try:
        req_log.info(
            "%s %s?%s -> %s in %.2fs%s",
            request.method, request.path, qs, resp.status_code, dt,
            f"  ({phase_s})" if phase_s else "",
        )
    except Exception:
        pass
    return resp


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    """Serve the service worker from root scope so it controls the whole site."""
    from flask import send_from_directory
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"  # so SW updates propagate immediately
    return resp


@app.route("/search")
def search_page():
    """Standalone semantic search interface."""
    return render_template("search.html")


@app.route("/api/semantic_search")
def api_semantic_search():
    """Semantic search over the pre-embedded article corpus.

    Query params:
        q: query text (required)
        hours: relative time window (e.g. 24, 168)
        date_from: YYYY-MM-DD
        date_to: YYYY-MM-DD
        domain: filter by source domain (ILIKE substring)
        language: e.g. 'en'
        page, per_page: pagination (default 1, 25)
        k: candidates to retrieve from FAISS (default 500)

    Returns ranked results with similarity scores. Public — no auth required.
    """
    import semantic_search

    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Missing q parameter"}), 400
    if len(q) > 500:
        return jsonify({"error": "Query too long (max 500 chars)"}), 400

    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = max(1, min(100, int(request.args.get("per_page", 25))))
        k = max(per_page * page, min(2000, int(request.args.get("k", 500))))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid pagination params"}), 400

    # Get top-K candidates from FAISS
    t0 = time.time()
    try:
        candidates = semantic_search.search(q, k=k)
    except Exception as e:
        return jsonify({"error": f"Search backend error: {e}"}), 503
    t_faiss = time.time() - t0

    if not candidates:
        return jsonify({
            "query": q, "articles": [], "total": 0,
            "page": page, "per_page": per_page, "pages": 0,
            "timing_ms": {"faiss": int(t_faiss * 1000)},
        })

    # Build a lookup of url -> score for ranking after DuckDB join
    score_by_url = {url: score for url, score in candidates}
    candidate_urls = list(score_by_url.keys())

    # Build filters
    conds = ["url IN ({})".format(",".join(["?"] * len(candidate_urls)))]
    params = list(candidate_urls)

    hours, df, dt = _parse_date_filters(request)
    if hours:
        conds.append("crawled_at >= ?")
        params.append(_hours_cutoff(hours))
    if df is not None:
        conds.append("crawled_at >= ?")
        params.append(df)
    if dt is not None:
        conds.append("crawled_at <= ?")
        params.append(dt)

    domain = (request.args.get("domain") or "").strip()
    if domain:
        conds.append("domain ILIKE ?")
        params.append(f"%{domain}%")

    language = (request.args.get("language") or "").strip()
    if language:
        conds.append("language = ?")
        params.append(language)

    # Query DuckDB for full article metadata
    con = get_db()
    if con is None:
        return jsonify({"error": "Database busy"}), 503

    cancel = _arm_statement_timeout(con)
    t1 = time.time()
    try:
        sql = (
            f"SELECT {GAL_COLS} FROM gal WHERE " + " AND ".join(conds)
        )
        rows = con.execute(sql, params).fetchall()
    except Exception as e:
        return jsonify({"error": f"DB query error: {e}"}), 500
    finally:
        try: cancel()
        except Exception: pass
        try: con.close()
        except Exception: pass
    t_db = time.time() - t1

    # Build result list, ranked by similarity score
    cols = ["url", "crawled_at", "domain", "outlet_name", "title",
            "image", "description", "language"]
    articles = []
    for row in rows:
        d = dict(zip(cols, row))
        d["score"] = score_by_url.get(d["url"], 0.0)
        ts = format_timestamp(d.get("crawled_at"))
        d["crawled_at_iso"] = ts.isoformat() if ts else None
        d["time_ago"] = time_ago(ts) if ts else None
        articles.append(d)
    articles.sort(key=lambda a: a["score"], reverse=True)

    total = len(articles)
    pages = (total + per_page - 1) // per_page
    start = (page - 1) * per_page
    page_articles = articles[start:start + per_page]

    return jsonify({
        "query": q,
        "articles": page_articles,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "k_searched": k,
        "timing_ms": {
            "faiss": int(t_faiss * 1000),
            "db": int(t_db * 1000),
        },
    })


@app.route("/api/views")
def api_views():
    """Return shared views + logged-in user's custom pills."""
    views = list(VIEWS)
    if current_user.is_authenticated:
        for pill in get_user_pills(current_user.id):
            job_status = pill.get("job_status", "queued")
            pill_type = pill.get("pill_type", "keyword")
            entry = {
                "id": f"custom-{pill['id']}",
                "name": pill["name"],
                "description": f"Custom pill: {pill['name']}",
                "kind": "tag_match",
                "tag_category": f"custom_{pill['id']}",
                "default_hours": 24,
                "custom": True,
                "pill_id": pill["id"],
                "pill_type": pill_type,
                "job_status": job_status,
                "article_count": pill.get("article_count", 0),
            }
            if pill_type == "semantic":
                entry["description_text"] = pill.get("description_text", "")
                entry["similarity_threshold"] = pill.get("similarity_threshold", 0.55)
            views.append(entry)
    return jsonify({
        "views": views,
        "authenticated": current_user.is_authenticated,
        "user": {
            "display_name": current_user.display_name,
            "is_admin": current_user.is_admin,
        } if current_user.is_authenticated else None,
    })


@app.route("/api/pill_info/<view_id>")
def api_pill_info(view_id):
    """Return what a pill is filtering on — keywords, themes, company
    patterns. Transparency endpoint so users can see the criteria."""
    view = find_view(view_id)
    if not view:
        return jsonify({"error": "Unknown view"}), 404

    info = {"id": view_id, "name": view["name"], "kind": view["kind"]}

    if view["kind"] == "fda_match":
        info["description"] = "Matches articles mentioning FDA-registered medical device companies by name."
        info["source"] = "fda_companies table (from FDA device registration database)"
        con = get_db()
        if con:
            try:
                sample = con.execute(
                    "SELECT firm_name FROM fda_companies ORDER BY product_count DESC LIMIT 20"
                ).fetchall()
                info["sample_companies"] = [r[0] for r in sample]
                info["total_companies"] = con.execute(
                    "SELECT count(*) FROM fda_companies"
                ).fetchone()[0]
            finally:
                con.close()

    elif view["kind"] == "tag_match":
        cat = view.get("tag_category", "")
        try:
            import importlib, sys
            parent = str(Path(__file__).resolve().parent.parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            tagger = importlib.import_module("pipeline.tagger")
            cat_conf = tagger.CATEGORIES.get(cat, {})
            info["keywords"] = sorted(cat_conf.get("keywords", []))
            info["gkg_theme_prefixes"] = cat_conf.get("gkg_theme_prefixes", [])
            info["scan_description"] = cat_conf.get("scan_description", False)
        except Exception:
            info["keywords"] = []

    return jsonify(info)


@app.route("/api/articles")
def api_articles():
    """Main API endpoint for article list with filtering.

    Query strategy:
    - If any entity-only filter is set (person/org/theme/location/tone) → GKG only.
    - Otherwise → UNION GKG + GAL, dedup by URL (prefer GKG), sort by date desc.
    """
    from flask import Response
    now = time.time()
    key = _feed_cache_key()
    # warm=1 (used by pipeline/warm_feed.py) forces a recompute + re-store so the
    # cache stays warm; it does NOT affect the cache key (not in _FEED_KEYS), so
    # the page's normal request hits the freshly-stored entry.
    warming = request.args.get("warm") == "1"
    hit = _feed_cache.get(key)
    if hit and hit[0] > now and not warming:
        g._req_phases["feed_cache"] = 0.0  # mark a cache hit in the log
        return Response(hit[1], mimetype="application/json")

    con = get_db()
    if con is None:
        return jsonify({
            "error": "Database is busy (backfill in progress). Try again shortly.",
            "articles": [], "total": 0, "page": 1, "per_page": 50, "pages": 0,
        }), 503

    cancel_timeout = _arm_statement_timeout(con)
    try:
        resp = _api_articles_inner(con)
        if getattr(resp, "status_code", 200) == 200:
            _feed_cache[key] = (now + _FEED_TTL_S, resp.get_data())
            if len(_feed_cache) > _FEED_CACHE_MAX:
                _feed_cache.pop(min(_feed_cache, key=lambda k: _feed_cache[k][0]), None)
        return resp
    except duckdb.InterruptException:
        return jsonify({
            "error": "Query timed out server-side. Try a narrower time window.",
            "articles": [], "total": 0, "page": 1, "per_page": 50, "pages": 0,
        }), 503
    finally:
        try: cancel_timeout()
        except Exception: pass
        try: con.close()
        except Exception: pass


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


@app.route("/event/<cluster_id>")
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


_stats_cache = {"at": 0.0, "data": None}
_STATS_TTL_S = 300  # full-table count(distinct) over 25M rows — fine to cache 5 min


@app.route("/api/stats")
def api_stats():
    """Quick stats for the header."""
    now = time.time()
    if _stats_cache["data"] and now - _stats_cache["at"] < _STATS_TTL_S:
        return jsonify(_stats_cache["data"])
    con = get_db()
    if con is None:
        return jsonify({"error": "Database busy", "total_articles": 0, "sources": 0, "latest_ago": "loading..."}), 503
    try:
        # GKG side — primary metadata (earliest/latest come from here)
        gkg_row = con.execute("""
            SELECT count(*), min("V1DATE"), max("V1DATE"),
                   count(DISTINCT "V2SOURCECOMMONNAME")
            FROM gkg
        """).fetchone()
        # GAL side — just counts + max crawled_at for freshness
        try:
            gal_row = con.execute(
                "SELECT count(*), max(crawled_at), count(DISTINCT domain) FROM gal"
            ).fetchone()
        except Exception:
            gal_row = (0, None, 0)
    finally:
        con.close()

    latest_dt = format_timestamp(gkg_row[2])
    earliest_dt = format_timestamp(gkg_row[1])
    # Prefer whichever corpus has the newer article for "latest" display.
    gal_latest = format_timestamp(gal_row[1]) if gal_row[1] else None
    if gal_latest and (not latest_dt or gal_latest > latest_dt):
        latest_dt = gal_latest

    data = {
        "total_articles": gkg_row[0] + gal_row[0],
        "gkg_articles": gkg_row[0],
        "gal_articles": gal_row[0],
        "earliest": str(gkg_row[1]),
        "latest": str(gkg_row[2]),
        "earliest_date": earliest_dt.strftime("%Y-%m-%d") if earliest_dt else None,
        "latest_date": latest_dt.strftime("%Y-%m-%d") if latest_dt else None,
        "earliest_display": earliest_dt.strftime("%b %d, %Y") if earliest_dt else None,
        "latest_display": latest_dt.strftime("%b %d, %Y %H:%M UTC") if latest_dt else None,
        "latest_ago": time_ago(latest_dt),
        "sources": gkg_row[3] + gal_row[2],
        "gkg_sources": gkg_row[3],
        "gal_sources": gal_row[2],
    }
    _stats_cache["at"] = now
    _stats_cache["data"] = data
    return jsonify(data)


@app.route("/api/perf", methods=["POST"])
def api_perf():
    """Receive RUM beacons (real-user front-end timings) and store a capped log."""
    try:
        from models import get_user_db
        payload = request.get_json(force=True, silent=True) or {}
        samples = payload.get("samples") or []
        if not samples:
            return ("", 204)
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for s in samples[:40]:
            try:
                rows.append((
                    ts, str(s.get("metric", ""))[:40], float(s.get("value") or 0),
                    str(s.get("view") or "")[:60], int(s.get("hours") or 0),
                ))
            except (TypeError, ValueError):
                continue
        if rows:
            uc = get_user_db()
            uc.executemany(
                "INSERT INTO perf_samples (ts, metric, value, view, hours) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            # keep the table bounded (~last 20k samples)
            uc.execute("DELETE FROM perf_samples WHERE id < (SELECT max(id) - 20000 FROM perf_samples)")
            uc.commit()
            uc.close()
    except Exception:
        pass
    return ("", 204)


# Facets cache for GAL — refreshed lazily with a 10-min TTL.
_facets_cache = {"at": 0.0, "data": None}
_FACETS_TTL_S = 600


@app.route("/api/gal_facets")
def api_gal_facets():
    """Return language and top-outlet facets for the GAL source. Cached
    for 10 minutes to avoid repeated count-distinct queries."""
    now = time.time()
    if _facets_cache["data"] and now - _facets_cache["at"] < _FACETS_TTL_S:
        return jsonify(_facets_cache["data"])

    con = get_db()
    if con is None:
        return jsonify({"error": "Database busy"}), 503
    try:
        # Top 30 languages by count. Limit to those with meaningful volume.
        lang_rows = con.execute("""
            SELECT COALESCE(NULLIF(language, ''), NULL) AS lang, count(*) AS n
            FROM gal
            WHERE lang IS NOT NULL
            GROUP BY lang
            HAVING n >= 1000
            ORDER BY n DESC
            LIMIT 30
        """).fetchall()
        outlet_rows = con.execute("""
            SELECT outlet_name, count(*) AS n
            FROM gal
            WHERE outlet_name IS NOT NULL AND length(outlet_name) > 0
            GROUP BY outlet_name
            ORDER BY n DESC
            LIMIT 100
        """).fetchall()
        total = con.execute("SELECT count(*) FROM gal").fetchone()[0]
    finally:
        con.close()

    data = {
        "languages": [
            {"code": r[0], "name": _LANG_NAMES.get(r[0], r[0]), "count": r[1]}
            for r in lang_rows
        ],
        "top_outlets": [{"name": r[0], "count": r[1]} for r in outlet_rows],
        "total": total,
    }
    _facets_cache["at"] = now
    _facets_cache["data"] = data
    return jsonify(data)


# Minimal ISO-code → human label map for the language dropdown. Covers the
# top languages found in GAL. Unknown codes fall back to the raw code.
_LANG_NAMES = {
    "en": "English", "es": "Spanish", "de": "German", "it": "Italian",
    "zh": "Chinese", "zh-Hant": "Chinese (Traditional)",
    "ru": "Russian", "tr": "Turkish", "el": "Greek",
    "fr": "French", "ar": "Arabic", "pt": "Portuguese",
    "vi": "Vietnamese", "ja": "Japanese", "ro": "Romanian",
    "id": "Indonesian", "uk": "Ukrainian", "et": "Estonian",
    "ko": "Korean", "sr": "Serbian", "pl": "Polish",
    "sq": "Albanian", "da": "Danish", "hi": "Hindi",
    "hr": "Croatian", "nl": "Dutch", "sv": "Swedish",
    "cs": "Czech", "fi": "Finnish", "no": "Norwegian",
    "bg": "Bulgarian", "sk": "Slovak", "he": "Hebrew",
    "th": "Thai", "fa": "Persian", "hu": "Hungarian",
}


@app.route("/api/top_entities")
def api_top_entities():
    """Top persons, orgs, themes, locations for filter suggestions."""
    con = get_db()
    if con is None:
        return jsonify({"error": "Database busy"}), 503
    hours = request.args.get("hours", 24, type=int)
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    cutoff_ts = int(cutoff.strftime("%Y%m%d%H%M%S"))

    # Sample recent articles and extract top entities
    rows = con.execute("""
        SELECT "V2ENHANCEDPERSONS", "V2ENHANCEDORGANIZATIONS",
               "V2ENHANCEDTHEMES", "V2ENHANCEDLOCATIONS", "V2SOURCECOMMONNAME"
        FROM gkg
        WHERE "V1DATE" >= ?
        ORDER BY "V1DATE" DESC
        LIMIT 2000
    """, [cutoff_ts]).fetchall()

    persons = {}
    orgs = {}
    themes = {}
    locations = {}
    sources = {}

    for row in rows:
        for name in parse_enhanced_list(row[0]):
            persons[name] = persons.get(name, 0) + 1
        for name in parse_enhanced_list(row[1]):
            orgs[name] = orgs.get(name, 0) + 1
        for name in parse_enhanced_list(row[2]):
            # Clean up theme names for display
            themes[name] = themes.get(name, 0) + 1
        for loc in parse_locations(row[3]):
            locations[loc["name"]] = locations.get(loc["name"], 0) + 1
        if row[4]:
            sources[row[4]] = sources.get(row[4], 0) + 1

    def top_n(d, n=20):
        return [{"name": k, "count": v} for k, v in
                sorted(d.items(), key=lambda x: -x[1])[:n]]

    con.close()
    return jsonify({
        "persons": top_n(persons),
        "organizations": top_n(orgs),
        "themes": top_n(themes),
        "locations": top_n(locations),
        "sources": top_n(sources),
    })


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        user_data = authenticate(email, password)
        if not user_data:
            flash("Invalid email or password.", "error")
            return render_template("login.html")
        if not user_data["is_approved"]:
            return redirect(url_for("pending"))
        login_user(User(user_data), remember=True)
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        if not email or not display_name or len(password) < 8:
            flash("All fields required. Password must be at least 8 characters.", "error")
            return render_template("register.html")
        if email_exists(email):
            flash("An account with this email already exists.", "error")
            return render_template("register.html")
        uid = create_user(email, display_name, password)
        user_data = authenticate(email, password)
        if user_data and user_data["is_approved"]:
            login_user(User(user_data), remember=True)
            return redirect(url_for("index"))
        return redirect(url_for("pending"))
    return render_template("register.html")


@app.route("/pending")
def pending():
    return render_template("pending.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "change_password":
            current = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            if len(new_pw) < 8:
                flash("New password must be at least 8 characters.", "error")
            else:
                from models import get_user_db
                from werkzeug.security import check_password_hash, generate_password_hash
                db = get_user_db()
                row = db.execute("SELECT password_hash FROM users WHERE id=?",
                                 (current_user.id,)).fetchone()
                if not row or not check_password_hash(row["password_hash"], current):
                    flash("Current password is incorrect.", "error")
                else:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (generate_password_hash(new_pw), current_user.id))
                    db.commit()
                    flash("Password updated.", "success")
                db.close()
        elif action == "change_name":
            new_name = request.form.get("display_name", "").strip()
            if new_name:
                from models import get_user_db
                db = get_user_db()
                db.execute("UPDATE users SET display_name=? WHERE id=?",
                           (new_name, current_user.id))
                db.commit()
                db.close()
                flash("Display name updated.", "success")
    return render_template("account.html")


@app.route("/portal")
@login_required
def portal():
    pills = get_user_pills(current_user.id)
    return render_template("portal.html", pills=pills, is_admin=current_user.is_admin)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route("/admin/users")
@login_required
def admin_users():
    if not current_user.is_admin:
        return redirect(url_for("index"))
    users = list_users()
    return render_template("admin.html", users=users)


@app.route("/admin/users/<int:user_id>/approve", methods=["POST"])
@login_required
def admin_approve(user_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    approve_user(user_id)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reject", methods=["POST"])
@login_required
def admin_reject(user_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    reject_user(user_id)
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# AI Briefing
# ---------------------------------------------------------------------------


@app.route("/api/briefing")
def api_briefing():
    """AI-generated briefing with optional SSE streaming."""
    from flask import Response

    view_id = (request.args.get("view") or "").strip()
    hours = request.args.get("hours", 24, type=int)
    stream = request.args.get("stream", "0") == "1"
    refresh = request.args.get("refresh") == "1"  # pre-warm: force regeneration

    cache_key = f"{view_id or '_all'}:{hours}"

    # Check cache first (both streaming and non-streaming)
    from models import get_user_db
    ucon = get_user_db()
    cached = ucon.execute(
        "SELECT briefing, article_count, generated_at, sources_json FROM briefing_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if cached and not refresh:
        age_s = (datetime.utcnow() - datetime.strptime(cached["generated_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        if age_s < BRIEFING_TTL_S:
            ucon.close()
            try:
                cached_sources = json.loads(cached["sources_json"]) if cached["sources_json"] else []
            except Exception:
                cached_sources = []
            if stream:
                # Send cached sources + briefing as SSE events
                def cached_stream():
                    yield f"data: {json.dumps({'sources': cached_sources})}\n\n"
                    yield f"data: {json.dumps({'text': cached['briefing'], 'done': True, 'cached': True, 'article_count': cached['article_count']})}\n\n"
                return Response(cached_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
            return jsonify({
                "briefing": cached["briefing"],
                "sources": cached_sources,
                "article_count": cached["article_count"],
                "generated_at": cached["generated_at"],
                "cached": True,
                "view": view_id or None,
                "hours": hours,
            })
    ucon.close()

    # Fetch deduped EVENTS for the selected window (no auto-widen — stays
    # consistent with the feed). If too sparse, "Not enough articles".
    sources, view_name, view_desc = _fetch_briefing_events(view_id, hours)
    if len(sources) < 2:
        _found = len(sources)
        if stream:
            def empty_stream(_f=_found):
                yield f"data: {json.dumps({'error': 'Not enough articles', 'found': _f, 'done': True})}\n\n"
            return Response(empty_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
        return jsonify({"briefing": None, "error": "Not enough articles", "found": _found})

    sources = sources[:BRIEFING_EVENT_LIMIT]
    # Compact map sent to the client to resolve [N] citation markers -> links.
    sources_payload = [
        {"n": i + 1, "link": s["link"], "outlet": s.get("outlet"),
         "title": s.get("title"), "n_sources": s.get("n_sources", 1)}
        for i, s in enumerate(sources)
    ]
    sources_json = json.dumps(sources_payload)

    if stream:
        # Stream tokens via SSE (sources first so the client can linkify [N]).
        def sse_stream():
            yield f"data: {json.dumps({'sources': sources_payload})}\n\n"
            full_text = []
            try:
                for chunk in _generate_briefing_stream(sources, view_name, view_desc, hours):
                    full_text.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
                return

            # Cache the completed briefing + its sources map
            briefing = "".join(full_text).strip()
            if briefing:
                generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    uc = get_user_db()
                    uc.execute(
                        "INSERT OR REPLACE INTO briefing_cache "
                        "(cache_key, briefing, article_count, generated_at, sources_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (cache_key, briefing, len(sources), generated_at, sources_json),
                    )
                    uc.commit()
                    uc.close()
                except Exception:
                    pass
            yield f"data: {json.dumps({'done': True, 'article_count': len(sources)})}\n\n"

        return Response(sse_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})

    # Non-streaming fallback
    briefing = _generate_briefing(sources, view_name, view_desc, hours)
    if not briefing:
        return jsonify({"briefing": None, "error": "Briefing generation unavailable"}), 503

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ucon = get_user_db()
    ucon.execute(
        "INSERT OR REPLACE INTO briefing_cache (cache_key, briefing, article_count, generated_at, sources_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (cache_key, briefing, len(sources), generated_at, sources_json),
    )
    ucon.commit()
    ucon.close()

    return jsonify({
        "briefing": briefing,
        "sources": sources_payload,
        "article_count": len(sources),
        "generated_at": generated_at,
        "cached": False,
        "view": view_id or None,
        "hours": hours,
    })


@app.route("/api/fda_events")
def api_fda_events():
    """Recent FDA device regulatory events (recalls, 510k clearances, enforcement).

    Query params:
        days: how many days back to return (default 30, max 90)
        type: filter by event_type ('recall', '510k', 'enforcement')
        firm: case-insensitive substring filter on firm_name
    """
    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    event_type = (request.args.get("type") or "").strip().lower()
    firm = (request.args.get("firm") or "").strip()

    cutoff_date = int(
        (datetime.utcnow() - timedelta(days=days)).strftime("%Y%m%d")
    )

    con = get_db()
    if con is None:
        return jsonify({"error": "Database busy"}), 503
    try:
        conds = ["event_date >= ?"]
        params: list = [cutoff_date]
        if event_type in ("recall", "510k", "enforcement"):
            conds.append("event_type = ?")
            params.append(event_type)
        if firm:
            conds.append("firm_name ILIKE ?")
            params.append(f"%{firm}%")
        where = " AND ".join(conds)
        rows = con.execute(
            f"SELECT event_id, event_type, event_date, firm_name, "
            f"       product_description, recall_class, reason_for_recall, status "
            f"FROM fda_regulatory_events "
            f"WHERE {where} "
            f"ORDER BY event_date DESC "
            f"LIMIT 200",
            params,
        ).fetchall()
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500
    finally:
        try: con.close()
        except Exception: pass

    events = []
    for r in rows:
        eid, etype, edate, fname, pdesc, rclass, reason, status = r
        events.append({
            "event_id": eid,
            "event_type": etype,
            "event_date": str(edate) if edate else None,
            "firm_name": fname,
            "product_description": pdesc,
            "recall_class": rclass,
            "reason_for_recall": reason,
            "status": status,
        })

    return jsonify({"events": events, "days": days, "total": len(events)})


# ---------------------------------------------------------------------------
# Custom pill API
# ---------------------------------------------------------------------------

@app.route("/api/pills", methods=["GET"])
@login_required
def api_pills_list():
    pills = get_user_pills(current_user.id)
    return jsonify({"pills": pills})


@app.route("/api/pills", methods=["POST"])
@login_required
def api_pills_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    pill_type = data.get("pill_type", "keyword")

    if not name or len(name) > 100:
        return jsonify({"error": "Name required (max 100 chars)"}), 400

    if pill_type == "semantic":
        description = (data.get("description") or "").strip()
        if not description or len(description) < 10:
            return jsonify({"error": "Description must be at least 10 characters"}), 400
        if len(description) > 2000:
            return jsonify({"error": "Description max 2000 characters"}), 400
        threshold = data.get("similarity_threshold", 0.55)
        try:
            threshold = max(0.3, min(0.8, float(threshold)))
        except (TypeError, ValueError):
            threshold = 0.55
        scan_days = data.get("scan_days", 7)
        try:
            scan_days = max(1, min(60, int(scan_days)))
        except (TypeError, ValueError):
            scan_days = 7

        # Embed the description via rainbow-boi
        import sys, struct
        _pipeline = str(Path(__file__).resolve().parent.parent / "pipeline")
        if _pipeline not in sys.path:
            sys.path.insert(0, _pipeline)
        try:
            from embedder import embed_query
            vec = embed_query(description)
            embedding_blob = struct.pack(f"{len(vec)}f", *vec)
        except Exception as e:
            return jsonify({"error": f"Embedding service error: {e}"}), 503

        pill_id = create_semantic_pill(
            current_user.id, name, description, embedding_blob, threshold,
            scan_days,
        )
        return jsonify({"pill_id": pill_id, "status": "queued", "pill_type": "semantic"}), 201

    # Keyword pill (default)
    keywords_raw = data.get("keywords", "")
    scan_desc = data.get("scan_description", True)

    if isinstance(keywords_raw, str):
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
    elif isinstance(keywords_raw, list):
        keywords = [str(k).strip().lower() for k in keywords_raw if str(k).strip()]
    else:
        keywords = []

    if len(keywords) < 2:
        return jsonify({"error": "At least 2 keywords required"}), 400
    if len(keywords) > 200:
        return jsonify({"error": "Max 200 keywords"}), 400

    pill_id = create_pill(current_user.id, name, keywords, scan_desc)
    return jsonify({"pill_id": pill_id, "status": "queued", "pill_type": "keyword"}), 201


@app.route("/api/pills/<pill_id>/status")
@login_required
def api_pill_status(pill_id):
    pill = get_pill(pill_id)
    if not pill or pill["user_id"] != current_user.id:
        return jsonify({"error": "not found"}), 404
    job = get_pill_job_status(pill_id)
    return jsonify({
        "id": pill_id,
        "name": pill["name"],
        "status": job["status"] if job else "unknown",
        "progress_pct": job["progress_pct"] if job else 0,
        "rows_scanned": job["rows_scanned"] if job else 0,
        "rows_matched": job["rows_matched"] if job else 0,
        "elapsed_seconds": job["elapsed_seconds"] if job else 0,
        "article_count": pill["article_count"],
    })


@app.route("/api/pills/<pill_id>", methods=["DELETE"])
@login_required
def api_pills_delete(pill_id):
    pill = get_pill(pill_id)
    if not pill:
        return jsonify({"error": "not found"}), 404
    if pill["user_id"] != current_user.id and not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    # Delete article_tags for this custom pill from DuckDB
    try:
        con = get_db()
        if con:
            con.execute(
                "DELETE FROM article_tags WHERE category = ?",
                [f"custom_{pill_id}"],
            )
            con.close()
    except Exception:
        pass
    delete_pill(pill_id)
    return jsonify({"deleted": pill_id})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8015, debug=True)
