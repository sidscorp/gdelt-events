"""Feed + stats + facets API routes."""

import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
from flask import Blueprint, request, jsonify, g
from flask_login import current_user

from views import VIEWS, find_view
from models import get_user_pills
from db import get_db, _arm_statement_timeout, _hours_cutoff
from parsers import (
    parse_enhanced_list, parse_locations, format_timestamp, time_ago,
)
from articles import (
    _api_articles_inner, _feed_cache, _feed_cache_key, _data_version,
    _FEED_TTL_S, _FEED_CACHE_MAX, _parse_date_filters, GAL_COLS,
)

bp = Blueprint("api_feed", __name__)


@bp.route("/api/semantic_search")
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


@bp.route("/api/views")
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
                "group": "My Pills",
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


@bp.route("/api/pill_info/<view_id>")
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
        info["description"] = view.get("description", "")
        try:
            import importlib, sys
            # repo root = routes/ -> dashboard/ -> repo (this broke silently
            # when the route moved from dashboard/app.py into dashboard/routes/)
            repo_root = str(Path(__file__).resolve().parent.parent.parent)
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            tagger = importlib.import_module("pipeline.tagger")
            cat_conf = tagger.CATEGORIES.get(cat, {})
            info["keywords"] = sorted(cat_conf.get("keywords", []))
            info["gkg_theme_prefixes"] = cat_conf.get("gkg_theme_prefixes", [])
            info["scan_description"] = cat_conf.get("scan_description", False)
            # Judge-gated pills (July 2026): expose the actual inclusion criteria
            if cat_conf.get("description"):
                info["judge_criteria"] = cat_conf["description"]
                info["judge_model"] = "gpt-oss-120b"
                info["judge_strict"] = bool(cat_conf.get("judge_strict"))
                info["neg_criteria"] = cat_conf.get("neg_description")
            if cat_conf.get("candidate_source") == "fda_match_cache":
                info["candidate_source"] = (
                    "Name matches against ~8,500 FDA-registered medical device "
                    "manufacturers (fda_match_cache)")
        except Exception as e:
            info["keywords"] = []
            info["error_detail"] = str(e)[:120]

    return jsonify(info)


@bp.route("/api/articles")
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


_stats_cache = {"at": 0.0, "data": None, "version": None}
_STATS_TTL_S = 1800  # fallback only — data_version is the real invalidator (see _feed_cache_key)


@bp.route("/api/stats")
def api_stats():
    """Quick stats for the header."""
    now = time.time()
    version = _data_version()
    if (_stats_cache["data"] and _stats_cache["version"] == version
            and now - _stats_cache["at"] < _STATS_TTL_S):
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
    _stats_cache["version"] = version
    return jsonify(data)


@bp.route("/api/perf", methods=["POST"])
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


@bp.route("/api/gal_facets")
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


@bp.route("/api/top_entities")
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
