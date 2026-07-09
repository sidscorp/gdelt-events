"""Article-feed engine for the GDELT dashboard.

Extracted verbatim from app.py: the GAL/GKG feed query builders, row
converters, view resolvers, fetchers, the event rollup, and the feed cache +
orchestrator that the /api/articles route in app.py drives.
"""

import time
from datetime import datetime

from flask import request, jsonify
from flask_login import current_user

from db import _hours_cutoff
from parsers import (
    parse_tone, parse_enhanced_list, parse_locations,
    extract_title, format_timestamp, time_ago,
)
from views import find_view
from models import get_pill
from webutil import _phase
from importance import compute_importance
# Cluster helpers live in briefing.py; importing them here (not the other way)
# keeps the dependency acyclic — briefing.py imports _window_events lazily.
from briefing import (
    _cluster_ids, _cluster_meta, _rep_from_members, BRIEFING_EVENT_BUFFER,
)


# Over-fetch multiplier for the within-buffer rollup (pills/FDA views), so a
# page still fills after duplicate cluster members are collapsed in Python.
ROLLUP_FETCH_FACTOR = 3


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

    # English-only: GKG records translated from another language carry a
    # populated V2TRANSLATIONINFO (srclc:…); natively-English ones leave it empty.
    if request.args.get("en_only") == "1":
        conditions.append('("V2TRANSLATIONINFO" IS NULL OR "V2TRANSLATIONINFO" = \'\')')

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
    if request.args.get("en_only") == "1":
        # English-only toggle wins over the (rarely-used) language dropdown.
        language = "en"
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


# In-process feed cache. The feed only changes on the ~15-min ingest cycle, so
# entries are keyed on the ingest-written data_version marker: a cached response
# stays valid until NEW DATA actually lands (the TTL is just a fallback), and a
# new ingest cycle invalidates every stale key at once.
_feed_cache: dict = {}
_FEED_TTL_S = 1800  # fallback only — the data_version cache-key component is the real invalidator
_FEED_CACHE_MAX = 256
_FEED_KEYS = (
    "view", "hours", "page", "per_page", "match_types", "q", "title", "description",
    "person", "org", "location", "theme", "domain", "outlet", "language",
    "date_from", "date_to", "sort", "order", "rollup", "source", "en_only",
)

_DATA_VERSION_PATH = None
_data_version_memo = (0.0, "0")  # (checked_at, version)


def _data_version():
    """mtime of data/data_version.txt (written by gdelt_ingest after each cycle
    that loaded files). Memoized 5s so cache hits never stat() more than
    occasionally. '0' when the marker doesn't exist yet."""
    global _DATA_VERSION_PATH, _data_version_memo
    now = time.time()
    checked_at, version = _data_version_memo
    if now - checked_at < 5:
        return version
    if _DATA_VERSION_PATH is None:
        from _paths import DATA_DIR
        _DATA_VERSION_PATH = DATA_DIR / "data_version.txt"
    try:
        version = str(int(_DATA_VERSION_PATH.stat().st_mtime))
    except OSError:
        version = "0"
    _data_version_memo = (now, version)
    return version


def _feed_cache_key():
    return _data_version() + "|" + "|".join(f"{k}={request.args.get(k, '')}" for k in _FEED_KEYS)


GKG_COLS = (
    '"GKGRECORDID", "V1DATE", "V2SOURCECOMMONNAME", "V2DOCUMENTIDENTIFIER", '
    '"V2ENHANCEDTHEMES", "V2ENHANCEDLOCATIONS", "V2ENHANCEDPERSONS", '
    '"V2ENHANCEDORGANIZATIONS", "V15TONE", "V2EXTRASXML", "V2SHARINGIMAGE"'
)


GAL_COLS = "url, crawled_at, domain, outlet_name, title, image, description, language"


# --- gal_recent routing ------------------------------------------------------
# gal_recent is a compact rolling ~8-day mirror of gal (maintained by
# pipeline/gal_loader.py, built once by pipeline/build_gal_recent.py, indexed
# on url + crawled_at). Window-bounded queries that stay within RECENT_HOURS
# scan ~3M rows instead of ~24M — same columns, same rows in the window.
RECENT_HOURS = 168
_gal_recent_exists = None


def _has_gal_recent(con):
    global _gal_recent_exists
    if _gal_recent_exists is None:
        try:
            _gal_recent_exists = bool(con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'gal_recent'"
            ).fetchone())
        except Exception:
            _gal_recent_exists = False
    return _gal_recent_exists


def _gal_table(con, cutoff=None, req=None):
    """Return 'gal_recent' when every lower time-bound of the query stays
    within RECENT_HOURS, else 'gal'. Pass either a precomputed YYYYMMDDHHMMSS
    integer cutoff or a request to derive the bounds from."""
    if not _has_gal_recent(con):
        return "gal"
    limit = _hours_cutoff(RECENT_HOURS)
    if req is not None:
        hours, df, _dt = _parse_date_filters(req)
        bounds = []
        if hours:
            bounds.append(_hours_cutoff(hours))
        if df is not None:
            bounds.append(df)
        if not bounds:
            return "gal_recent"  # _build_gal_where defaults to a 7-day window
        cutoff = max(bounds)
    if cutoff is None:
        return "gal"
    return "gal_recent" if cutoff >= limit else "gal"


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


def _order_mode(request) -> str:
    """Feed ordering: 'importance' (default) ranks the window's events by the
    composite importance score; 'date' is the classic reverse-chronological feed
    (with `sort=oldest` flipping to ascending)."""
    o = (request.args.get("order") or "importance").strip().lower()
    return o if o in ("importance", "date") else "importance"


# Text/entity filters the importance path doesn't (yet) reimplement — when any
# is set we fall back to the date path, which fully supports them.
_IMP_FALLBACK_FILTERS = (
    "q", "title", "description", "person", "org", "location", "theme",
    "domain", "outlet", "language",
)


def _has_text_filters(request) -> bool:
    return any((request.args.get(k) or "").strip() for k in _IMP_FALLBACK_FILTERS)


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
    if request.args.get("en_only") == "1":
        language = "en"
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
            SELECT article_id, crawled_at, matched_name, medical_specialties, match_type
            FROM fda_match_cache
            WHERE {cache_where}
            ORDER BY crawled_at {sd}
            LIMIT ?
        )
        SELECT {GKG_COLS}, top_ids.matched_name, top_ids.medical_specialties, top_ids.match_type
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
        base = r[:-3]
        match_info[base[0]] = (r[-3] or "", r[-2] or "", r[-1] or "")
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
        SELECT article_id, crawled_at, matched_name, medical_specialties, match_type
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
    gal_meta = {r[0]: (r[2], r[3], r[4]) for r in top_gal}
    placeholders = ",".join(["?"] * len(urls))
    # cache_where's own time bound (unlike _build_gal_where, this function has
    # NO implicit default — an unbounded request stays unbounded, so cutoff
    # must come from the same hours/df/dt this function already computed).
    _view_cutoff = _hours_cutoff(hours) if hours else (df if df is not None else None)
    lookup = _phase("gal_join", lambda: con.execute(
        f"SELECT {GAL_COLS} FROM {_gal_table(con, cutoff=_view_cutoff)} WHERE url IN ({placeholders})",
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
        name, spec, mtype = gal_meta[url]
        match_info[url] = (name or "", spec or "", mtype or "")
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
    sd = _sort_dir(request)
    fetch_n = (page * per_page) + per_page
    # Deterministic order (time, then doc-id/url) so the top-N is a stable
    # prefix across page sizes — keeps pagination + event rollup consistent.
    rows = _phase("gkg_query", lambda: con.execute(
        f'SELECT {GKG_COLS} FROM gkg WHERE {gkg_where} '
        f'ORDER BY "V1DATE" {sd}, "V2DOCUMENTIDENTIFIER" {sd} LIMIT ?',
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
    tbl = _gal_table(con, req=request)
    fetch_n = (page * per_page) + per_page
    rows = _phase("gal_query", lambda: con.execute(
        f"SELECT {GAL_COLS} FROM {tbl} WHERE {gal_where} "
        f"ORDER BY crawled_at {sd}, url {sd} LIMIT ?",
        gal_params + [fetch_n],
    ).fetchall())
    total = _phase("gal_total", lambda: con.execute(
        f"SELECT count(*) FROM {tbl} WHERE {gal_where}", gal_params,
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
        return (0 if extra_where else total), []

    urls = [r[0] for r in top_gal]
    placeholders = ",".join(["?"] * len(urls))
    # Same no-implicit-default caveat as _fetch_gal_view above.
    _tag_cutoff = _hours_cutoff(hours) if hours else (df if df is not None else None)
    lookup = _phase("gal_join", lambda: con.execute(
        f"SELECT {GAL_COLS} FROM {_gal_table(con, cutoff=_tag_cutoff)} WHERE url IN ({placeholders})",
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

    # Count and page are separate concerns: filter the whole candidate window so
    # `total` reflects every match (not just one page), then slice the page.
    matched = []
    for url in urls:
        row = by_url.get(url)
        if row is None or not _matches(row):
            continue
        matched.append(tuple(row))
    if extra_where:
        total = len(matched)
    rows = matched[:fetch_n]
    return total, rows


# ---------------------------------------------------------------------------
# Importance ordering (window-wide event ranking).
#
# Where the date feed shows the newest ~150 articles, the importance feed pulls
# the WHOLE window's candidate events (top clusters by coverage + recent
# breaking singletons for the global view; recent tagged/fda rows for a pill),
# dedups them into one card per event, and ranks by the composite importance
# score (coverage + velocity + recency — see importance.py). The AI briefing
# consumes the SAME gatherer (_window_events), so the summary reflects the top
# feed cards. Inspired by the M4ESTRO paper's Hazard/Disruption Relevance Score.
# ---------------------------------------------------------------------------

IMP_GLOBAL_CLUSTERS = 40   # top clusters by coverage seeded into the global view
IMP_GLOBAL_SINGLES = 40    # recent un-clustered breaking items mixed in

# GAL_COLS aliased to the `g.` table used by the tag/fda joins.
_GAL_G_COLS = ", ".join("g." + c.strip() for c in GAL_COLS.split(","))


def _card_from_cluster(cid, rep_url, title, image, size, first_seen, latest_seen, mjson):
    """Build a feed/briefing card from a clusters-table row (global view)."""
    outlet, desc = _rep_from_members(mjson)
    dt = format_timestamp(latest_seen)
    return {
        "source_type": "gal",
        "id": rep_url or cid,
        "url": rep_url or "",
        "title": title or "",
        "description": desc,
        "outlet_name": outlet,
        "source": outlet or "",
        "image": image or None,
        "timestamp": dt.isoformat() if dt else None,
        "time_ago": time_ago(dt),
        "sort_key": int(latest_seen) if latest_seen else 0,
        "n_sources": size,
        "first_seen": first_seen,
        "latest_seen": latest_seen,
        "cluster_id": cid,
        "variant_count": size,
        "event_url": f"/event/{cid}",
        "variants": [],
        "persons": [], "organizations": [], "themes": [], "locations": [], "tone": None,
    }


def _cards_from_gal_rows(con, rows):
    """Dedup GAL_COLS rows into one card per event-cluster (newest member kept,
    others collapsed as variants), annotated with coverage + temporal fields
    (n_sources / first_seen / latest_seen) for importance scoring."""
    urls = [r[0] for r in rows if r[0]]
    cmap = _cluster_ids(con, urls)                  # url -> cid
    meta = _cluster_meta(con, set(cmap.values()))   # cid -> (size, first_seen, latest_seen)
    seen: dict = {}
    cards = []
    for r in rows:
        art = _gal_row_to_article(r)
        url = art.get("url")
        if not url or len((art.get("title") or "").strip()) <= 10:
            continue
        cid = cmap.get(url)
        key = cid or url
        rep = seen.get(key)
        if rep is None:
            if cid and cid in meta:
                size, fs, ls = meta[cid]
                art["n_sources"] = size
                art["first_seen"] = fs
                art["latest_seen"] = ls
                art["cluster_id"] = cid
                art["variant_count"] = size
                art["event_url"] = f"/event/{cid}"
            else:
                art["n_sources"] = 1
                art["latest_seen"] = art.get("sort_key")
            art["variants"] = []
            seen[key] = art
            cards.append(art)
        else:
            rep["variants"].append({
                "url": url,
                "outlet_name": art.get("outlet_name") or art.get("source"),
                "title": art.get("title"),
                "time_ago": art.get("time_ago"),
            })
    return cards


def _global_window_events(con, cutoff, en_only=True):
    """Global view candidates: the window's most-covered events (clusters table,
    so it spans the whole window) + recent breaking singletons."""
    cards = []
    try:
        rows = con.execute(
            "SELECT cluster_id, rep_url, title, image, size, first_seen, latest_seen, members_json "
            "FROM clusters WHERE latest_seen >= ? AND status = 'active' "
            "ORDER BY size DESC LIMIT ?",
            [cutoff, IMP_GLOBAL_CLUSTERS],
        ).fetchall()
        for row in rows:
            card = _card_from_cluster(*row)
            if len((card.get("title") or "").strip()) > 10:
                cards.append(card)
    except Exception:
        pass  # clusters table absent -> singletons only
    try:
        lang_cond = "AND language = 'en' " if en_only else ""
        srows = con.execute(
            f"SELECT {GAL_COLS} FROM {_gal_table(con, cutoff=cutoff)} "
            f"WHERE crawled_at >= ? {lang_cond}AND title IS NOT NULL "
            f"ORDER BY crawled_at DESC LIMIT ?",
            [cutoff, IMP_GLOBAL_SINGLES * 2],
        ).fetchall()
        cmap = _cluster_ids(con, [r[0] for r in srows])
        added = 0
        for r in srows:
            if added >= IMP_GLOBAL_SINGLES:
                break
            art = _gal_row_to_article(r)
            if cmap.get(art.get("url")) or len((art.get("title") or "").strip()) <= 10:
                continue  # clustered -> already represented above
            art["n_sources"] = 1
            art["latest_seen"] = art.get("sort_key")
            art["variants"] = []
            cards.append(art)
            added += 1
    except Exception:
        pass
    return cards


def _window_events(con, view, cutoff, en_only=True):
    """Window-wide candidate events for a view + time-window, deduped into one
    card per event and RANKED BY IMPORTANCE (coverage + velocity + recency).

    Shared by the feed's Importance sort and the AI briefing so the two stay
    coherent. Cards carry both rich feed fields (image/description/time_ago/
    variants/rollup) and the scoring inputs (n_sources/first_seen/latest_seen).
    """
    kind = (view or {}).get("kind")
    if view is None:
        cards = _global_window_events(con, cutoff, en_only=en_only)
    elif kind == "tag_match":
        lang_cond = "AND g.language = 'en' " if en_only else ""
        rows = con.execute(
            f"SELECT {_GAL_G_COLS} FROM article_tags t JOIN {_gal_table(con, cutoff=cutoff)} g ON g.url = t.article_id "
            f"WHERE t.category = ? AND t.source_type = 'gal' AND t.crawled_at >= ? {lang_cond}"
            f"ORDER BY t.crawled_at DESC LIMIT ?",
            [view["tag_category"], cutoff, BRIEFING_EVENT_BUFFER],
        ).fetchall()
        cards = _cards_from_gal_rows(con, rows)
    elif kind == "fda_match":
        lang_cond = "AND g.language = 'en' " if en_only else ""
        rows = con.execute(
            f"SELECT {_GAL_G_COLS} FROM fda_match_cache f JOIN {_gal_table(con, cutoff=cutoff)} g ON g.url = f.article_id "
            f"WHERE f.source_type = 'gal' AND f.crawled_at >= ? {lang_cond}"
            f"ORDER BY f.crawled_at DESC LIMIT ?",
            [cutoff, BRIEFING_EVENT_BUFFER],
        ).fetchall()
        cards = _cards_from_gal_rows(con, rows)
    else:
        lang_cond = "AND language = 'en' " if en_only else ""
        rows = con.execute(
            f"SELECT {GAL_COLS} FROM {_gal_table(con, cutoff=cutoff)} "
            f"WHERE crawled_at >= ? {lang_cond}AND title IS NOT NULL "
            f"ORDER BY crawled_at DESC LIMIT ?",
            [cutoff, BRIEFING_EVENT_BUFFER],
        ).fetchall()
        cards = _cards_from_gal_rows(con, rows)
    return compute_importance(cards)


def _importance_feed(con, view, page, per_page, offset):
    """Assemble one feed page from the window's importance-ranked events."""
    hours, df, dt = _parse_date_filters(request)
    if hours:
        cutoff = _hours_cutoff(hours)
    elif df is not None:
        cutoff = df
    else:
        cutoff = _hours_cutoff(168)
    en_only = request.args.get("en_only") == "1"

    cards = _window_events(con, view, cutoff, en_only=en_only)
    if dt is not None:  # honor an explicit upper date bound
        cards = [c for c in cards
                 if (c.get("latest_seen") or c.get("sort_key") or 0) <= dt]

    total = len(cards)
    page_cards = cards[offset:offset + per_page]
    for a in page_cards:
        for k in ("sort_key", "_imp", "first_seen", "latest_seen"):
            a.pop(k, None)

    _attach_inclusion_reason(con, request, page_cards)

    return jsonify({
        "articles": page_cards,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })


def _attach_inclusion_reason(con, req, articles):
    """Transparency: for tag-based pill views, attach WHY each article is in
    the pill — article_tags.matched_via ('judge'/'keyword'/'theme'/'semantic')
    and matched_detail (judge verdict + cosine, or the matched keyword). One
    indexed IN-query over the page's ≤50 urls; no-op on the global feed."""
    view = find_view((req.args.get("view") or "").strip())
    if not view or view.get("kind") != "tag_match" or not articles:
        return
    try:
        urls = [a.get("url") for a in articles if a.get("url")]
        if not urls:
            return
        ph = ",".join(["?"] * len(urls))
        why = {}
        for u, via, detail in con.execute(
            f"SELECT article_id, any_value(matched_via), any_value(matched_detail) "
            f"FROM article_tags WHERE category = ? AND source_type='gal' "
            f"AND article_id IN ({ph}) GROUP BY article_id",
            [view["tag_category"]] + urls,
        ).fetchall():
            why[u] = {"via": via, "detail": detail}
        for a in articles:
            w = why.get(a.get("url"))
            if w:
                a["inclusion"] = w
    except Exception:
        pass  # transparency data is best-effort, never break the feed


def _api_articles_inner(con):
    """GAL-only reading feed: web articles with summaries + thumbnail images,
    rolled up into events via the cluster tables. (The GKG/entity-based view was
    retired 2026-06-27 — GAL carries descriptions+images and the clusters are
    100% GAL URLs. See memory: revisit GKG/GSG as a future project.)

    Ordering: `order=importance` (default) ranks the whole window's events by the
    composite importance score; `order=date` is the classic reverse-chronological
    feed. Text/entity filters force the date path (which fully supports them)."""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 50, type=int))
    offset = (page - 1) * per_page

    view = _resolve_view(request)
    view_kind = (view or {}).get("kind")
    is_fda = view_kind == "fda_match"
    is_tag = view_kind == "tag_match"
    view_match_info: dict[str, tuple[str, str]] = {}

    # Importance ordering: window-wide ranked events (skipped when a text/entity
    # filter is active — those fall through to the fully-featured date path).
    if _order_mode(request) == "importance" and not _has_text_filters(request):
        return _importance_feed(con, view, page, per_page, offset)

    # Event rollup: over-fetch, then collapse duplicate cluster members in
    # Python, keeping each cluster's newest member (deterministic time,url order
    # gives global dedup with no expensive anti-join).
    rollup_on = request.args.get("rollup", "1") != "0"
    fetch_pp = per_page * ROLLUP_FETCH_FACTOR if rollup_on else per_page

    if is_fda:
        gal_total, gal_rows, mi = _fetch_gal_view(con, view, request, fetch_pp, page)
        view_match_info.update(mi)
    elif is_tag:
        gal_total, gal_rows = _fetch_gal_tag_view(con, view, request, fetch_pp, page)
    else:
        gal_total, gal_rows = _fetch_gal_plain(con, request, fetch_pp, page)

    seen_urls = set()
    merged = []
    for r in gal_rows:
        art = _gal_row_to_article(r)
        if art["url"] and art["url"] not in seen_urls:
            seen_urls.add(art["url"])
            mi = view_match_info.get(art["url"])
            if mi:
                art["matched_name"], art["matched_specialty"] = mi[0], mi[1]
                if len(mi) > 2:
                    art["matched_type"] = mi[2]
            merged.append(art)
    merged.sort(key=lambda a: (a["sort_key"], a.get("url") or ""),
                reverse=(_sort_dir(request) == "DESC"))
    total = gal_total

    # Roll up near-duplicate / same-event articles into single cards (no-op if
    # the cluster tables don't exist yet, so this is safe before backfill).
    if rollup_on:
        merged = _rollup_articles(con, merged)

    articles = merged[offset:offset + per_page]

    for a in articles:
        a.pop("sort_key", None)

    _attach_inclusion_reason(con, request, articles)

    return jsonify({
        "articles": articles,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })


def _enrich_descriptions(con, articles):
    """Backfill a short summary onto articles that lack one. GKG records carry no
    description, but ~91% have a matching GAL row (by URL) that does — one indexed
    IN-list lookup over the page of articles, only on a feed cache miss."""
    need = [a["url"] for a in articles
            if a.get("url") and not (a.get("description") or "").strip()]
    if not need:
        return
    try:
        placeholders = ",".join(["?"] * len(need))
        rows = con.execute(
            f"SELECT url, description FROM gal "
            f"WHERE url IN ({placeholders}) AND description IS NOT NULL AND description <> ''",
            need,
        ).fetchall()
        desc_by_url = {u: d for (u, d) in rows}
        for a in articles:
            if not (a.get("description") or "").strip():
                d = desc_by_url.get(a.get("url"))
                if d:
                    a["description"] = d.strip()[:300]
    except Exception:
        pass


def _rollup_articles(con, merged):
    """Collapse articles that belong to the same materialized event cluster.

    For each cluster present on the page, emit ONE representative card (the
    best/first member present, time-sorted) annotated with the FULL cluster
    size and the on-page variants. Articles with no cluster pass through.
    Safe no-op when the cluster tables are absent (e.g. before backfill).
    """
    if not merged:
        return merged
    urls = [a["url"] for a in merged if a.get("url")]
    if not urls:
        return merged

    url2cid = {}
    try:
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            ph = ",".join(["?"] * len(chunk))
            for u, cid in con.execute(
                f"SELECT article_url, cluster_id FROM cluster_members WHERE article_url IN ({ph})",
                chunk,
            ).fetchall():
                url2cid[u] = cid
    except Exception:
        return merged  # cluster tables not present -> no rollup
    if not url2cid:
        return merged

    cids = list(set(url2cid.values()))
    cluster_meta = {}
    for i in range(0, len(cids), 500):
        chunk = cids[i:i + 500]
        ph = ",".join(["?"] * len(chunk))
        for cid, size in con.execute(
            f"SELECT cluster_id, size FROM clusters WHERE cluster_id IN ({ph})",
            chunk,
        ).fetchall():
            cluster_meta[cid] = size

    out = []
    card_by_cid = {}
    for a in merged:
        cid = url2cid.get(a.get("url"))
        if not cid or cid not in cluster_meta:
            out.append(a)
            continue
        rep = card_by_cid.get(cid)
        if rep is None:
            # first member on this page becomes the visible representative
            a["cluster_id"] = cid
            a["variant_count"] = cluster_meta[cid]
            a["event_url"] = f"/event/{cid}"
            a["variants"] = []
            card_by_cid[cid] = a
            out.append(a)
        else:
            rep["variants"].append({
                "url": a.get("url"),
                "outlet_name": a.get("outlet_name") or a.get("source"),
                "title": a.get("title"),
                "time_ago": a.get("time_ago"),
            })
    return out
