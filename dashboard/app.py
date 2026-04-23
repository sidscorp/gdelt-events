"""GDELT News Dashboard — Tufte-inspired breaking news viewer."""

import json
import logging
import logging.handlers
import re
import threading
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

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gdelt.duckdb"
LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

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

# Per-connection bounds. Each per-request DuckDB connection gets a tight
# memory limit so a runaway query can't balloon the waitress process to
# 10 GB (observed this session). Without this, per-request connections
# accumulated page cache across requests and the proc drifted OOM-ward.
CONN_MEMORY_LIMIT = "1500MB"
CONN_THREADS = 4
STATEMENT_TIMEOUT_S = 35  # dashboard aborts its own slow queries at 35s


def get_db(max_retries=3):
    """Get a read-only DuckDB connection, retrying briefly on lock conflicts.

    Each connection has a bounded memory_limit + thread count to prevent
    the dashboard process from accumulating DuckDB page cache across
    requests. Read-only mode still allows multiple readers in parallel.
    """
    for attempt in range(max_retries):
        try:
            con = duckdb.connect(str(DB_PATH))
            try:
                con.execute(f"SET memory_limit='{CONN_MEMORY_LIMIT}'")
                con.execute(f"SET threads={CONN_THREADS}")
            except Exception:
                pass  # older DuckDB versions may differ; not fatal
            return con
        except duckdb.IOException:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
    return None


def _arm_statement_timeout(con):
    """Install a threading.Timer that calls con.interrupt() after
    STATEMENT_TIMEOUT_S. Returns a cancel function."""
    def _kill():
        try:
            con.interrupt()
        except Exception:
            pass
    t = threading.Timer(STATEMENT_TIMEOUT_S, _kill)
    t.daemon = True
    t.start()
    return t.cancel


@app.before_request
def _req_start():
    g._req_t0 = time.perf_counter()
    g._req_phases = {}


@app.after_request
def _req_end(resp):
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


def _phase(name, fn):
    """Time a callable and record it into g._req_phases."""
    t0 = time.perf_counter()
    try:
        return fn()
    finally:
        g._req_phases[name] = time.perf_counter() - t0


def parse_tone(tone_str):
    """Parse V15TONE: tone,pos,neg,polarity,activity,selfgroup,wordcount."""
    if not tone_str:
        return None
    try:
        parts = tone_str.split(",")
        return {
            "tone": float(parts[0]),
            "positive": float(parts[1]),
            "negative": float(parts[2]),
            "polarity": float(parts[3]),
            "wordcount": int(float(parts[6])) if len(parts) > 6 else 0,
        }
    except (ValueError, IndexError):
        return None


def parse_enhanced_list(field, name_only=True):
    """Parse semicolon-delimited enhanced fields like persons, orgs, themes.
    Format: Name,charoffset;Name,charoffset; ...
    Returns deduplicated list of names.
    """
    if not field:
        return []
    items = set()
    for entry in field.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if name_only and "," in entry:
            name = entry.rsplit(",", 1)[0].strip()
        else:
            name = entry
        if name:
            items.add(name)
    return sorted(items)


def parse_locations(field):
    """Parse enhanced locations: type#name#country#adm1#adm2#lat#long#featureID#offset;
    Returns list of {name, country, lat, long}.
    """
    if not field:
        return []
    locs = []
    seen = set()
    for entry in field.split(";"):
        parts = entry.strip().split("#")
        if len(parts) < 5:
            continue
        name = parts[1].strip()
        if not name or name in seen:
            continue
        seen.add(name)
        country = parts[2].strip()
        try:
            lat = float(parts[5]) if len(parts) > 5 and parts[5] else None
            lon = float(parts[6]) if len(parts) > 6 and parts[6] else None
        except ValueError:
            lat = lon = None
        locs.append({"name": name, "country": country, "lat": lat, "lon": lon})
    return locs


def extract_title(extras_xml):
    """Extract PAGE_TITLE from V2EXTRASXML field."""
    if not extras_xml:
        return None
    m = re.search(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", extras_xml)
    return m.group(1).strip() if m else None


def format_timestamp(ts):
    """Convert GDELT timestamp (YYYYMMDDHHmmss as int) to datetime."""
    if not ts:
        return None
    s = str(int(ts))
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def time_ago(dt):
    """Human-readable relative time with minute precision."""
    if not dt:
        return ""
    delta = datetime.utcnow() - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    mins_remaining = minutes % 60
    if hours < 24:
        if mins_remaining == 0:
            return f"{hours}h ago"
        return f"{hours}h {mins_remaining}m ago"
    days = hours // 24
    hrs_remaining = hours % 24
    if hrs_remaining == 0:
        return f"{days}d ago"
    return f"{days}d {hrs_remaining}h ago"


@app.route("/")
def index():
    return render_template("index.html")


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


def _parse_date_filters(request):
    """Parse common time/date args. Returns (hours, date_from_int, date_to_int)."""
    hours = request.args.get("hours", type=int)
    date_from_int = None
    date_to_int = None
    date_from = request.args.get("date_from")
    if date_from:
        try:
            date_from_int = int(datetime.strptime(date_from, "%Y-%m-%d").strftime("%Y%m%d000000"))
        except ValueError:
            pass
    date_to = request.args.get("date_to")
    if date_to:
        try:
            date_to_int = int(datetime.strptime(date_to, "%Y-%m-%d").strftime("%Y%m%d235959"))
        except ValueError:
            pass
    return hours, date_from_int, date_to_int


def _hours_cutoff(hours):
    if not hours:
        return None
    return int((datetime.utcnow() - timedelta(hours=hours)).strftime("%Y%m%d%H%M%S"))


def _resolve_view(request) -> dict | None:
    """Look up the requested view by id — checks static VIEWS first,
    then custom pills for the logged-in user."""
    view_id = request.args.get("view", "").strip()
    if not view_id:
        return None
    static = find_view(view_id)
    if static:
        return static
    # Check custom pills: view_id is "custom-<pill_id>"
    if view_id.startswith("custom-") and current_user.is_authenticated:
        pill_id = view_id[len("custom-"):]
        pill = get_pill(pill_id)
        if pill and pill["user_id"] == current_user.id:
            return {
                "id": view_id,
                "name": pill["name"],
                "kind": "tag_match",
                "tag_category": f"custom_{pill_id}",
                "default_hours": 24,
                "custom": True,
            }
    return None


def _build_gkg_where(request):
    """Build GKG WHERE clause from request args."""
    conditions = []
    params = []

    hours, df, dt = _parse_date_filters(request)
    if hours:
        conditions.append('"V1DATE" >= ?')
        params.append(_hours_cutoff(hours))
    if df is not None:
        conditions.append('"V1DATE" >= ?')
        params.append(df)
    if dt is not None:
        conditions.append('"V1DATE" <= ?')
        params.append(dt)

    q = request.args.get("q", "").strip()
    if q:
        conditions.append(
            '(regexp_extract("V2EXTRASXML", \'<PAGE_TITLE>(.*?)</PAGE_TITLE>\', 1) ILIKE ? '
            'OR "V2ENHANCEDPERSONS" ILIKE ? '
            'OR "V2ENHANCEDORGANIZATIONS" ILIKE ? '
            'OR "V2ALLNAMES" ILIKE ? '
            'OR "V2SOURCECOMMONNAME" ILIKE ?)'
        )
        params.extend([f"%{q}%"] * 5)

    title = request.args.get("title", "").strip()
    if title:
        conditions.append(
            'regexp_extract("V2EXTRASXML", \'<PAGE_TITLE>(.*?)</PAGE_TITLE>\', 1) ILIKE ?'
        )
        params.append(f"%{title}%")

    person = request.args.get("person", "").strip()
    if person:
        conditions.append('("V2ENHANCEDPERSONS" ILIKE ? OR "V2ALLNAMES" ILIKE ?)')
        params.extend([f"%{person}%", f"%{person}%"])

    org = request.args.get("org", "").strip()
    if org:
        conditions.append('("V2ENHANCEDORGANIZATIONS" ILIKE ? OR "V2ALLNAMES" ILIKE ?)')
        params.extend([f"%{org}%", f"%{org}%"])

    theme = request.args.get("theme", "").strip()
    if theme:
        conditions.append('("V2ENHANCEDTHEMES" ILIKE ? OR "V1THEMES" ILIKE ?)')
        params.extend([f"%{theme}%", f"%{theme}%"])

    domain = request.args.get("domain", "").strip()
    if domain:
        conditions.append('"V2SOURCECOMMONNAME" ILIKE ?')
        params.append(f"%{domain}%")

    location = request.args.get("location", "").strip()
    if location:
        conditions.append('"V2ENHANCEDLOCATIONS" ILIKE ?')
        params.append(f"%{location}%")

    tone_min = request.args.get("tone_min", type=float)
    tone_max = request.args.get("tone_max", type=float)
    if tone_min is not None or tone_max is not None:
        conditions.append('"V15TONE" IS NOT NULL')
        if tone_min is not None:
            conditions.append('CAST(split_part("V15TONE", \',\', 1) AS DOUBLE) >= ?')
            params.append(tone_min)
        if tone_max is not None:
            conditions.append('CAST(split_part("V15TONE", \',\', 1) AS DOUBLE) <= ?')
            params.append(tone_max)

    where = " AND ".join(conditions) if conditions else "1=1"
    return where, params


def _build_gal_where(request):
    """Build GAL WHERE clause. GAL has no entities so person/org/theme/location
    filters are not applicable (caller should skip GAL in those cases).
    """
    conditions = []
    params = []

    hours, df, dt = _parse_date_filters(request)
    if hours:
        conditions.append("crawled_at >= ?")
        params.append(_hours_cutoff(hours))
    if df is not None:
        conditions.append("crawled_at >= ?")
        params.append(df)
    if dt is not None:
        conditions.append("crawled_at <= ?")
        params.append(dt)
    # Safety net: if no time filter at all, default to 7 days to prevent
    # full-table ILIKE scans on 25M rows (which take 35s+ and block ingest).
    if not hours and df is None and dt is None:
        conditions.append("crawled_at >= ?")
        params.append(_hours_cutoff(168))

    q = request.args.get("q", "").strip()
    if q:
        conditions.append("(title ILIKE ? OR description ILIKE ? OR domain ILIKE ? OR outlet_name ILIKE ?)")
        params.extend([f"%{q}%"] * 4)

    title = request.args.get("title", "").strip()
    if title:
        conditions.append("title ILIKE ?")
        params.append(f"%{title}%")

    description = request.args.get("description", "").strip()
    if description:
        conditions.append("description ILIKE ?")
        params.append(f"%{description}%")

    # The `source` filter param is about the news outlet (domain), not the
    # GAL/GKG table selector — those are different concepts. The request
    # sends ?domain= for outlet domain filtering.
    domain = request.args.get("domain", "").strip()
    if domain:
        conditions.append("domain ILIKE ?")
        params.append(f"%{domain}%")

    outlet = request.args.get("outlet", "").strip()
    if outlet:
        conditions.append("outlet_name ILIKE ?")
        params.append(f"%{outlet}%")

    language = request.args.get("language", "").strip()
    if language:
        # Exact match — language codes are short ISO codes (en, es, de, …).
        conditions.append("language = ?")
        params.append(language)

    where = " AND ".join(conditions) if conditions else "1=1"
    return where, params


def _view_cache_cte(view: dict | None, source_type: str, cutoff_ts: int | None = None) -> tuple[str, list]:
    """Build a CTE for the fda_match_cache pre-filter when a fda_match view
    is active. Returns (cte_sql, params).

    Empty strings when no view is active — caller should prepend these to the
    query FROM clause (as a `WITH ... ` prefix) only when non-empty.

    Using the cache as the outer DRIVER (rather than a semi-join in the FROM
    clause) is dramatically faster: top-N ORDER BY on the small cache, then
    a point-lookup JOIN back into the massive source table. 44s -> 40ms.
    """
    if not (view and view.get("kind") == "fda_match"):
        return "", []
    where = "source_type = ?"
    params: list = [source_type]
    if cutoff_ts is not None:
        where += " AND crawled_at >= ?"
        params.append(cutoff_ts)
    return where, params


def _gkg_entity_filters_set(request):
    """True if any filter is set that requires GKG entity fields."""
    return any(request.args.get(k, "").strip()
               for k in ("person", "org", "theme", "location", "tone_min", "tone_max"))


def _gkg_row_to_article(row):
    (gkg_id, v1date, source_name, url, themes, locations, persons,
     orgs, tone_str, extras_xml, sharing_image) = row
    dt = format_timestamp(v1date)
    return {
        "source_type": "gkg",
        "id": gkg_id,
        "timestamp": dt.isoformat() if dt else None,
        "time_ago": time_ago(dt),
        "sort_key": int(v1date) if v1date else 0,
        "source": source_name or "",
        "url": url or "",
        "title": extract_title(extras_xml) or "",
        "description": None,
        "outlet_name": None,
        "persons": parse_enhanced_list(persons),
        "organizations": parse_enhanced_list(orgs),
        "themes": parse_enhanced_list(themes),
        "locations": parse_locations(locations),
        "tone": parse_tone(tone_str),
        "image": sharing_image or None,
    }


def _gal_row_to_article(row):
    (url, crawled_at, domain, outlet_name, title, image, description, language) = row
    dt = format_timestamp(crawled_at)
    return {
        "source_type": "gal",
        "id": url,
        "timestamp": dt.isoformat() if dt else None,
        "time_ago": time_ago(dt),
        "sort_key": int(crawled_at) if crawled_at else 0,
        "source": domain or outlet_name or "",
        "url": url or "",
        "title": title or "",
        "description": description,
        "outlet_name": outlet_name,
        "language": language,
        "persons": [],
        "organizations": [],
        "themes": [],
        "locations": [],
        "tone": None,
        "image": image or None,
    }


@app.route("/api/articles")
def api_articles():
    """Main API endpoint for article list with filtering.

    Query strategy:
    - If any entity-only filter is set (person/org/theme/location/tone) → GKG only.
    - Otherwise → UNION GKG + GAL, dedup by URL (prefer GKG), sort by date desc.
    """
    con = get_db()
    if con is None:
        return jsonify({
            "error": "Database is busy (backfill in progress). Try again shortly.",
            "articles": [], "total": 0, "page": 1, "per_page": 50, "pages": 0,
        }), 503

    cancel_timeout = _arm_statement_timeout(con)
    try:
        return _api_articles_inner(con)
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


GKG_COLS = (
    '"GKGRECORDID", "V1DATE", "V2SOURCECOMMONNAME", "V2DOCUMENTIDENTIFIER", '
    '"V2ENHANCEDTHEMES", "V2ENHANCEDLOCATIONS", "V2ENHANCEDPERSONS", '
    '"V2ENHANCEDORGANIZATIONS", "V15TONE", "V2EXTRASXML", "V2SHARINGIMAGE"'
)

GAL_COLS = "url, crawled_at, domain, outlet_name, title, image, description, language"


def _resolve_source(request) -> str:
    """Top-level source tab. One of gal | gkg | all. Defaults to gal."""
    s = (request.args.get("source") or "").strip().lower()
    if s in ("gal", "gkg", "all"):
        return s
    return "gal"


_VALID_MATCH_TYPES = frozenset({"legal", "stripped", "contextual"})


def _sort_dir(request) -> str:
    """'DESC' or 'ASC' based on the sort query param."""
    s = (request.args.get("sort") or "").strip().lower()
    return "ASC" if s == "oldest" else "DESC"


def _resolve_match_types(request) -> list[str]:
    """Parse match_types param. Default ['legal']. Accepts 'all'."""
    raw = (request.args.get("match_types") or "").strip().lower()
    if not raw:
        return ["legal"]
    if raw == "all":
        return sorted(_VALID_MATCH_TYPES)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    valid = [p for p in parts if p in _VALID_MATCH_TYPES]
    return valid or ["legal"]


def _extra_gal_filters(request) -> tuple[str, list]:
    """Non-time GAL filters for stacking on the FDA view cache query."""
    conds, params = [], []
    q = (request.args.get("q") or "").strip()
    if q:
        conds.append(
            "(title ILIKE ? OR description ILIKE ? OR domain ILIKE ? OR outlet_name ILIKE ?)"
        )
        params.extend([f"%{q}%"] * 4)
    title = (request.args.get("title") or "").strip()
    if title:
        conds.append("title ILIKE ?"); params.append(f"%{title}%")
    description = (request.args.get("description") or "").strip()
    if description:
        conds.append("description ILIKE ?"); params.append(f"%{description}%")
    domain = (request.args.get("domain") or "").strip()
    if domain:
        conds.append("domain ILIKE ?"); params.append(f"%{domain}%")
    outlet = (request.args.get("outlet") or "").strip()
    if outlet:
        conds.append("outlet_name ILIKE ?"); params.append(f"%{outlet}%")
    language = (request.args.get("language") or "").strip()
    if language:
        conds.append("language = ?"); params.append(language)
    return (" AND ".join(conds), params)


def _extra_gkg_filters(request) -> tuple[str, list]:
    """Non-time GKG filters for stacking on the FDA view cache query."""
    conds, params = [], []
    q = (request.args.get("q") or "").strip()
    if q:
        conds.append(
            '(regexp_extract("V2EXTRASXML", \'<PAGE_TITLE>(.*?)</PAGE_TITLE>\', 1) ILIKE ? '
            'OR "V2ENHANCEDPERSONS" ILIKE ? '
            'OR "V2ENHANCEDORGANIZATIONS" ILIKE ? '
            'OR "V2ALLNAMES" ILIKE ? '
            'OR "V2SOURCECOMMONNAME" ILIKE ?)'
        )
        params.extend([f"%{q}%"] * 5)
    title = (request.args.get("title") or "").strip()
    if title:
        conds.append(
            'regexp_extract("V2EXTRASXML", \'<PAGE_TITLE>(.*?)</PAGE_TITLE>\', 1) ILIKE ?'
        )
        params.append(f"%{title}%")
    person = (request.args.get("person") or "").strip()
    if person:
        conds.append('("V2ENHANCEDPERSONS" ILIKE ? OR "V2ALLNAMES" ILIKE ?)')
        params.extend([f"%{person}%", f"%{person}%"])
    org = (request.args.get("org") or "").strip()
    if org:
        conds.append('("V2ENHANCEDORGANIZATIONS" ILIKE ? OR "V2ALLNAMES" ILIKE ?)')
        params.extend([f"%{org}%", f"%{org}%"])
    location = (request.args.get("location") or "").strip()
    if location:
        conds.append('"V2ENHANCEDLOCATIONS" ILIKE ?')
        params.append(f"%{location}%")
    theme = (request.args.get("theme") or "").strip()
    if theme:
        conds.append('("V2ENHANCEDTHEMES" ILIKE ? OR "V1THEMES" ILIKE ?)')
        params.extend([f"%{theme}%", f"%{theme}%"])
    domain = (request.args.get("domain") or "").strip()
    if domain:
        conds.append('"V2SOURCECOMMONNAME" ILIKE ?')
        params.append(f"%{domain}%")
    return (" AND ".join(conds), params)


def _fetch_gkg_view(con, view, request, per_page, page) -> tuple[int, list, dict]:
    """FDA view → GKG cache-as-driver path."""
    hours, df, dt = _parse_date_filters(request)
    match_types = _resolve_match_types(request)
    mt_placeholders = ",".join(["?"] * len(match_types))
    cache_conds: list[str] = [f"match_type IN ({mt_placeholders})"]
    cache_params: list = list(match_types)
    if hours:
        cache_conds.append("crawled_at >= ?"); cache_params.append(_hours_cutoff(hours))
    if df is not None:
        cache_conds.append("crawled_at >= ?"); cache_params.append(df)
    if dt is not None:
        cache_conds.append("crawled_at <= ?"); cache_params.append(dt)
    cache_where = " AND ".join(["source_type = 'gkg'"] + cache_conds)
    sd = _sort_dir(request)

    total = _phase("gkg_count", lambda: con.execute(
        f"SELECT count(*) FROM fda_match_cache WHERE {cache_where}",
        cache_params,
    ).fetchone()[0])

    extra_where, extra_params = _extra_gkg_filters(request)
    fetch_n = (page * per_page) + per_page
    cache_limit = min(fetch_n * 5, 500) if extra_where else fetch_n

    join_where_suffix = f" AND ({extra_where})" if extra_where else ""
    raw = _phase("gkg_join", lambda: con.execute(
        f"""
        WITH top_ids AS (
            SELECT article_id, crawled_at, matched_name, medical_specialties
            FROM fda_match_cache
            WHERE {cache_where}
            ORDER BY crawled_at {sd}
            LIMIT ?
        )
        SELECT {GKG_COLS}, top_ids.matched_name, top_ids.medical_specialties
        FROM top_ids
        INNER JOIN gkg ON gkg."GKGRECORDID" = top_ids.article_id
        WHERE 1=1{join_where_suffix}
        ORDER BY top_ids.crawled_at {sd}
        LIMIT ?
        """,
        cache_params + [cache_limit] + extra_params + [fetch_n],
    ).fetchall())

    rows, match_info = [], {}
    for r in raw:
        base = r[:-2]
        match_info[base[0]] = (r[-2] or "", r[-1] or "")
        rows.append(base)
    if extra_where:
        total = len(rows)
    return total, rows, match_info


def _fetch_gal_view(con, view, request, per_page, page) -> tuple[int, list, dict]:
    """FDA view → GAL cache-as-driver path with idx_gal_url point-lookup."""
    sd = _sort_dir(request)
    hours, df, dt = _parse_date_filters(request)
    match_types = _resolve_match_types(request)
    mt_placeholders = ",".join(["?"] * len(match_types))
    cache_conds: list[str] = [f"match_type IN ({mt_placeholders})"]
    cache_params: list = list(match_types)
    if hours:
        cache_conds.append("crawled_at >= ?"); cache_params.append(_hours_cutoff(hours))
    if df is not None:
        cache_conds.append("crawled_at >= ?"); cache_params.append(df)
    if dt is not None:
        cache_conds.append("crawled_at <= ?"); cache_params.append(dt)
    cache_where = " AND ".join(["source_type = 'gal'"] + cache_conds)

    total = _phase("gal_count", lambda: con.execute(
        f"SELECT count(*) FROM fda_match_cache WHERE {cache_where}",
        cache_params,
    ).fetchone()[0])

    # Wider cache pull when filters set; post-filter in Python to avoid
    # DuckDB ART index pathology on large IN-clauses.
    extra_where, extra_params = _extra_gal_filters(request)
    fetch_n = (page * per_page) + per_page
    cache_limit = min(fetch_n * 5, 500) if extra_where else fetch_n

    top_gal = _phase("gal_cache", lambda: con.execute(
        f"""
        SELECT article_id, crawled_at, matched_name, medical_specialties
        FROM fda_match_cache
        WHERE {cache_where}
        ORDER BY crawled_at {sd}
        LIMIT ?
        """,
        cache_params + [cache_limit],
    ).fetchall())

    if not top_gal:
        return total, [], {}

    urls = [r[0] for r in top_gal]
    gal_meta = {r[0]: (r[2], r[3]) for r in top_gal}
    placeholders = ",".join(["?"] * len(urls))
    lookup = _phase("gal_join", lambda: con.execute(
        f"SELECT {GAL_COLS} FROM gal WHERE url IN ({placeholders})",
        urls,
    ).fetchall())

    by_url = {row[0]: row for row in lookup}

    def _matches(row):
        if not extra_where:
            return True
        _url, _ts, domain, outlet, title, _img, description, language = row
        args = request.args
        q = (args.get("q") or "").strip().lower()
        if q:
            hay = " ".join(str(x or "") for x in (title, description, domain, outlet)).lower()
            if q not in hay:
                return False
        t = (args.get("title") or "").strip().lower()
        if t and t not in (title or "").lower():
            return False
        d = (args.get("description") or "").strip().lower()
        if d and d not in (description or "").lower():
            return False
        dom = (args.get("domain") or "").strip().lower()
        if dom and dom not in (domain or "").lower():
            return False
        out = (args.get("outlet") or "").strip().lower()
        if out and out not in (outlet or "").lower():
            return False
        lang = (args.get("language") or "").strip()
        if lang and lang != language:
            return False
        return True

    rows, match_info = [], {}
    matched_count = 0
    for url in urls:
        row = by_url.get(url)
        if row is None:
            continue
        if not _matches(row):
            continue
        name, spec = gal_meta[url]
        match_info[url] = (name or "", spec or "")
        rows.append(tuple(row))
        matched_count += 1
        if matched_count >= fetch_n:
            break
    if extra_where:
        total = matched_count  # honest count for what we actually returned
    return total, rows, match_info


def _fetch_gkg_plain(con, request, per_page, page) -> tuple[int, list]:
    """Non-view GKG path with filter support."""
    gkg_where, gkg_params = _build_gkg_where(request)
    fetch_n = (page * per_page) + per_page
    rows = _phase("gkg_query", lambda: con.execute(
        f'SELECT {GKG_COLS} FROM gkg WHERE {gkg_where} '
        f'ORDER BY "V1DATE" {_sort_dir(request)} LIMIT ?',
        gkg_params + [fetch_n],
    ).fetchall())
    total = _phase("gkg_total", lambda: con.execute(
        f"SELECT count(*) FROM gkg WHERE {gkg_where}", gkg_params,
    ).fetchone()[0])
    return total, rows


def _fetch_gal_plain(con, request, per_page, page) -> tuple[int, list]:
    """Non-view GAL path with filter support."""
    gal_where, gal_params = _build_gal_where(request)
    sd = _sort_dir(request)
    fetch_n = (page * per_page) + per_page
    rows = _phase("gal_query", lambda: con.execute(
        f"SELECT {GAL_COLS} FROM gal WHERE {gal_where} "
        f"ORDER BY crawled_at {sd} LIMIT ?",
        gal_params + [fetch_n],
    ).fetchall())
    total = _phase("gal_total", lambda: con.execute(
        f"SELECT count(*) FROM gal WHERE {gal_where}", gal_params,
    ).fetchone()[0])
    return total, rows


def _fetch_gkg_tag_view(con, view, request, per_page, page) -> tuple[int, list]:
    """Tag-based view → GKG path. Cache-as-driver from article_tags."""
    sd = _sort_dir(request)
    category = view["tag_category"]
    hours, df, dt = _parse_date_filters(request)
    cache_conds: list[str] = [
        "category = ?", "source_type = 'gkg'"
    ]
    cache_params: list = [category]
    if hours:
        cache_conds.append("crawled_at >= ?"); cache_params.append(_hours_cutoff(hours))
    if df is not None:
        cache_conds.append("crawled_at >= ?"); cache_params.append(df)
    if dt is not None:
        cache_conds.append("crawled_at <= ?"); cache_params.append(dt)
    cache_where = " AND ".join(cache_conds)

    total = _phase("gkg_count", lambda: con.execute(
        f"SELECT count(*) FROM article_tags WHERE {cache_where}",
        cache_params,
    ).fetchone()[0])

    fetch_n = (page * per_page) + per_page
    raw = _phase("gkg_join", lambda: con.execute(
        f"""
        WITH top_ids AS (
            SELECT article_id, crawled_at
            FROM article_tags
            WHERE {cache_where}
            ORDER BY crawled_at {sd}
            LIMIT ?
        )
        SELECT {GKG_COLS}
        FROM top_ids
        INNER JOIN gkg ON gkg."GKGRECORDID" = top_ids.article_id
        ORDER BY top_ids.crawled_at {sd}
        """,
        cache_params + [fetch_n],
    ).fetchall())
    return total, raw


def _fetch_gal_tag_view(con, view, request, per_page, page) -> tuple[int, list]:
    """Tag-based view → GAL path. Two-step point-lookup."""
    sd = _sort_dir(request)
    category = view["tag_category"]
    hours, df, dt = _parse_date_filters(request)
    cache_conds: list[str] = [
        "category = ?", "source_type = 'gal'"
    ]
    cache_params: list = [category]
    if hours:
        cache_conds.append("crawled_at >= ?"); cache_params.append(_hours_cutoff(hours))
    if df is not None:
        cache_conds.append("crawled_at >= ?"); cache_params.append(df)
    if dt is not None:
        cache_conds.append("crawled_at <= ?"); cache_params.append(dt)
    cache_where = " AND ".join(cache_conds)

    total = _phase("gal_count", lambda: con.execute(
        f"SELECT count(*) FROM article_tags WHERE {cache_where}",
        cache_params,
    ).fetchone()[0])

    extra_where, _ = _extra_gal_filters(request)
    fetch_n = (page * per_page) + per_page
    cache_limit = min(fetch_n * 5, 500) if extra_where else fetch_n

    top_gal = _phase("gal_cache", lambda: con.execute(
        f"""
        SELECT article_id, crawled_at
        FROM article_tags
        WHERE {cache_where}
        ORDER BY crawled_at {sd}
        LIMIT ?
        """,
        cache_params + [cache_limit],
    ).fetchall())

    if not top_gal:
        return total, []

    urls = [r[0] for r in top_gal]
    placeholders = ",".join(["?"] * len(urls))
    lookup = _phase("gal_join", lambda: con.execute(
        f"SELECT {GAL_COLS} FROM gal WHERE url IN ({placeholders})",
        urls,
    ).fetchall())

    # Preserve cache ordering
    by_url = {row[0]: row for row in lookup}

    def _matches(row):
        if not extra_where:
            return True
        _url, _ts, domain, outlet, title, _img, description, language = row
        args = request.args
        q = (args.get("q") or "").strip().lower()
        if q and q not in " ".join(str(x or "") for x in (title, description, domain, outlet)).lower():
            return False
        t = (args.get("title") or "").strip().lower()
        if t and t not in (title or "").lower():
            return False
        d = (args.get("description") or "").strip().lower()
        if d and d not in (description or "").lower():
            return False
        dom = (args.get("domain") or "").strip().lower()
        if dom and dom not in (domain or "").lower():
            return False
        out = (args.get("outlet") or "").strip().lower()
        if out and out not in (outlet or "").lower():
            return False
        lang = (args.get("language") or "").strip()
        if lang and lang != language:
            return False
        return True

    rows = []
    for url in urls:
        row = by_url.get(url)
        if row is None:
            continue
        if not _matches(row):
            continue
        rows.append(tuple(row))
        if len(rows) >= fetch_n:
            break
    if extra_where:
        total = len(rows)
    return total, rows


def _api_articles_inner(con):
    """Unified feed: always queries both GAL and GKG, deduplicates by URL
    (preferring GKG for richer metadata). When entity filters (person, org,
    theme, location, tone) are active, only GKG articles are returned since
    GAL lacks entity extraction."""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 50, type=int))
    offset = (page - 1) * per_page

    view = _resolve_view(request)
    view_kind = (view or {}).get("kind")
    is_fda = view_kind == "fda_match"
    is_tag = view_kind == "tag_match"
    view_match_info: dict[str, tuple[str, str]] = {}

    has_entity_filters = _gkg_entity_filters_set(request)

    gkg_rows: list = []
    gal_rows: list = []
    gkg_total = 0
    gal_total = 0

    # Always fetch GKG
    if is_fda:
        gkg_total, gkg_rows, mi = _fetch_gkg_view(con, view, request, per_page, page)
        view_match_info.update(mi)
    elif is_tag:
        gkg_total, gkg_rows = _fetch_gkg_tag_view(con, view, request, per_page, page)
    else:
        gkg_total, gkg_rows = _fetch_gkg_plain(con, request, per_page, page)

    # Fetch GAL only if no entity filters are active
    if not has_entity_filters:
        try:
            if is_fda:
                gal_total, gal_rows, mi = _fetch_gal_view(con, view, request, per_page, page)
                view_match_info.update(mi)
            elif is_tag:
                gal_total, gal_rows = _fetch_gal_tag_view(con, view, request, per_page, page)
            else:
                gal_total, gal_rows = _fetch_gal_plain(con, request, per_page, page)
        except Exception:
            gal_total, gal_rows = 0, []

    # Merge and deduplicate — prefer GKG version when same URL exists in both
    seen_urls = set()
    merged = []
    for r in gkg_rows:
        art = _gkg_row_to_article(r)
        if art["url"] and art["url"] not in seen_urls:
            seen_urls.add(art["url"])
            mi = view_match_info.get(art["id"]) or view_match_info.get(art["url"])
            if mi:
                art["matched_name"], art["matched_specialty"] = mi
            merged.append(art)
    for r in gal_rows:
        art = _gal_row_to_article(r)
        if art["url"] and art["url"] not in seen_urls:
            seen_urls.add(art["url"])
            mi = view_match_info.get(art["url"])
            if mi:
                art["matched_name"], art["matched_specialty"] = mi
            merged.append(art)
    merged.sort(key=lambda a: a["sort_key"], reverse=(_sort_dir(request) == "DESC"))
    total = gkg_total + gal_total
    articles = merged[offset:offset + per_page]

    for a in articles:
        a.pop("sort_key", None)

    return jsonify({
        "articles": articles,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })


@app.route("/api/stats")
def api_stats():
    """Quick stats for the header."""
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

    return jsonify({
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
    })


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

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
BRIEFING_MODEL = "google/gemini-2.5-flash"
BRIEFING_TTL_S = 900  # 15 minutes
_OPENROUTER_KEY_PATH = Path(__file__).resolve().parent.parent / "data" / ".openrouter_key"


def _get_openrouter_key():
    import os
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    if _OPENROUTER_KEY_PATH.exists():
        return _OPENROUTER_KEY_PATH.read_text().strip()
    return None


def _build_briefing_prompt(headlines: list[str], view_name: str, view_desc: str,
                            hours: int) -> str:
    """Build a time-aware, context-rich briefing prompt."""
    numbered = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))

    # Human-readable time range
    if hours <= 1:
        time_label = "the last hour"
    elif hours <= 24:
        time_label = f"the last {hours} hours"
    elif hours <= 72:
        time_label = f"the last {hours // 24} days"
    elif hours <= 168:
        time_label = "the last week"
    else:
        time_label = f"the last {hours // 24} days"

    topic_context = ""
    if view_name and view_name != "Global News":
        topic_context = (
            f'This feed monitors "{view_name}" — {view_desc}. '
            f"Focus your analysis on developments relevant to this topic. "
        )

    return (
        f"You are a senior news intelligence analyst writing a briefing for a decision-maker. "
        f"Below are {len(headlines)} article headlines from {time_label}, drawn from "
        f"44,000+ global news sources monitored in real-time.\n\n"
        f"{topic_context}"
        f"Write a structured intelligence briefing in this format:\n\n"
        f"Start with a brief header stating the topic and time window "
        f"(e.g., 'AI & Machine Learning — Last 24 Hours' or 'Global News — Last 7 Days'). "
        f"The topic is \"{view_name}\" and the time window is {time_label}.\n\n"
        f"Then write a 2-3 sentence executive summary of the overall landscape.\n\n"
        f"Then provide 3-5 key highlights as bullet points (use • as the bullet character). "
        f"Each highlight should be a single clear sentence naming specific companies, countries, "
        f"people, or figures. Focus on what changed, what's escalating, what was announced, "
        f"and what a strategist should watch.\n\n"
        f"End with a single sentence on what to watch next.\n\n"
        f"Be specific and concrete. No filler. No hedging. "
        f"Use markdown formatting for emphasis and structure.\n\n"
        f"Headlines:\n{numbered}"
    )


def _generate_briefing_stream(headlines, view_name, view_desc, hours):
    """Stream briefing tokens from OpenRouter as SSE."""
    from urllib.request import Request, urlopen

    key = _get_openrouter_key()
    if not key:
        return

    prompt = _build_briefing_prompt(headlines, view_name, view_desc, hours)
    payload = json.dumps({
        "model": BRIEFING_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 800,
        "temperature": 0.3,
        "stream": True,
    }).encode()

    req = Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    with urlopen(req, timeout=60) as resp:
        for line in resp:
            line = line.decode("utf-8").strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except (json.JSONDecodeError, IndexError, KeyError):
                continue


def _generate_briefing(headlines, view_name, view_desc, hours):
    """Non-streaming briefing generation (for caching)."""
    parts = []
    try:
        for chunk in _generate_briefing_stream(headlines, view_name, view_desc, hours):
            parts.append(chunk)
    except Exception as e:
        req_log.warning("Briefing generation failed: %s", e)
        return None
    return "".join(parts).strip() if parts else None


def _fetch_briefing_headlines(view_id, hours):
    """Fetch article titles for a briefing. Returns (headlines, view_name, view_desc)."""
    con = get_db()
    if con is None:
        return [], "Global News", ""

    try:
        cutoff = _hours_cutoff(hours) if hours else _hours_cutoff(168)
        view = find_view(view_id) if view_id else None
        view_name = view["name"] if view else "Global News"
        view_desc = view.get("description", "") if view else "All articles from 44K+ sources worldwide"

        if view and view.get("kind") == "tag_match":
            cat = view["tag_category"]
            rows = con.execute(
                "SELECT g.title FROM article_tags t "
                "JOIN gal g ON g.url = t.article_id "
                "WHERE t.category = ? AND t.source_type = 'gal' AND t.crawled_at >= ? "
                "ORDER BY t.crawled_at DESC LIMIT 75",
                [cat, cutoff],
            ).fetchall()
        elif view and view.get("kind") == "fda_match":
            rows = con.execute(
                "SELECT g.title FROM fda_match_cache f "
                "JOIN gal g ON g.url = f.article_id "
                "WHERE f.source_type = 'gal' AND f.crawled_at >= ? "
                "ORDER BY f.crawled_at DESC LIMIT 75",
                [cutoff],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT title FROM gal WHERE crawled_at >= ? AND language = 'en' "
                "AND title IS NOT NULL ORDER BY crawled_at DESC LIMIT 75",
                [cutoff],
            ).fetchall()
    except Exception:
        rows = []
    finally:
        try:
            con.close()
        except Exception:
            pass

    headlines = [r[0] for r in rows if r[0] and len(r[0].strip()) > 10]
    return headlines, view_name, view_desc


@app.route("/api/briefing")
def api_briefing():
    """AI-generated briefing with optional SSE streaming."""
    from flask import Response

    view_id = (request.args.get("view") or "").strip()
    hours = request.args.get("hours", 24, type=int)
    stream = request.args.get("stream", "0") == "1"

    cache_key = f"{view_id or '_all'}:{hours}"

    # Check cache first (both streaming and non-streaming)
    from models import get_user_db
    ucon = get_user_db()
    cached = ucon.execute(
        "SELECT briefing, article_count, generated_at FROM briefing_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if cached:
        age_s = (datetime.utcnow() - datetime.strptime(cached["generated_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        if age_s < BRIEFING_TTL_S:
            ucon.close()
            if stream:
                # Send cached result as a single SSE event
                def cached_stream():
                    yield f"data: {json.dumps({'text': cached['briefing'], 'done': True, 'cached': True, 'article_count': cached['article_count']})}\n\n"
                return Response(cached_stream(), mimetype="text/event-stream")
            return jsonify({
                "briefing": cached["briefing"],
                "article_count": cached["article_count"],
                "generated_at": cached["generated_at"],
                "cached": True,
                "view": view_id or None,
                "hours": hours,
            })
    ucon.close()

    # Fetch headlines
    headlines, view_name, view_desc = _fetch_briefing_headlines(view_id, hours)
    if len(headlines) < 5:
        if stream:
            def empty_stream():
                yield f"data: {json.dumps({'error': 'Not enough articles', 'done': True})}\n\n"
            return Response(empty_stream(), mimetype="text/event-stream")
        return jsonify({"briefing": None, "error": "Not enough articles"})

    if stream:
        # Stream tokens via SSE
        def sse_stream():
            full_text = []
            try:
                for chunk in _generate_briefing_stream(headlines[:75], view_name, view_desc, hours):
                    full_text.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
                return

            # Cache the completed briefing
            briefing = "".join(full_text).strip()
            if briefing:
                generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    uc = get_user_db()
                    uc.execute(
                        "INSERT OR REPLACE INTO briefing_cache "
                        "(cache_key, briefing, article_count, generated_at) VALUES (?, ?, ?, ?)",
                        (cache_key, briefing, len(headlines), generated_at),
                    )
                    uc.commit()
                    uc.close()
                except Exception:
                    pass
            yield f"data: {json.dumps({'done': True, 'article_count': len(headlines)})}\n\n"

        return Response(sse_stream(), mimetype="text/event-stream")

    # Non-streaming fallback
    briefing = _generate_briefing(headlines[:75], view_name, view_desc, hours)
    if not briefing:
        return jsonify({"briefing": None, "error": "Briefing generation unavailable"}), 503

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ucon = get_user_db()
    ucon.execute(
        "INSERT OR REPLACE INTO briefing_cache (cache_key, briefing, article_count, generated_at) "
        "VALUES (?, ?, ?, ?)",
        (cache_key, briefing, len(headlines), generated_at),
    )
    ucon.commit()
    ucon.close()

    return jsonify({
        "briefing": briefing,
        "article_count": len(headlines),
        "generated_at": generated_at,
        "cached": False,
        "view": view_id or None,
        "hours": hours,
    })


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
