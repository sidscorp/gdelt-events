"""Article-feed engine for the GDELT dashboard.

Extracted verbatim from app.py: the GAL/GKG feed query builders, row
converters, view resolvers, fetchers, the event rollup, and the feed cache +
orchestrator that the /api/articles route in app.py drives.
"""

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


# In-process feed cache. The feed only changes on the 15-min ingest, so caching
# the assembled response for ~90s makes repeat loads + the page's 60s auto-refresh
# instant, and skips the DuckDB connection + query entirely on a hit.
_feed_cache: dict = {}
_FEED_TTL_S = 180  # feed only changes on the ~15-min ingest/cluster cycle; warm_feed.py refreshes every ~2.5 min
_FEED_CACHE_MAX = 256
_FEED_KEYS = (
    "view", "hours", "page", "per_page", "match_types", "q", "title", "description",
    "person", "org", "location", "theme", "domain", "outlet", "language",
    "date_from", "date_to", "sort", "rollup", "source", "en_only",
)


def _feed_cache_key():
    return "|".join(f"{k}={request.args.get(k, '')}" for k in _FEED_KEYS)


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
    fetch_n = (page * per_page) + per_page
    rows = _phase("gal_query", lambda: con.execute(
        f"SELECT {GAL_COLS} FROM gal WHERE {gal_where} "
        f"ORDER BY crawled_at {sd}, url {sd} LIMIT ?",
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
    """GAL-only reading feed: web articles with summaries + thumbnail images,
    rolled up into events via the cluster tables. (The GKG/entity-based view was
    retired 2026-06-27 — GAL carries descriptions+images and the clusters are
    100% GAL URLs. See memory: revisit GKG/GSG as a future project.)"""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 50, type=int))
    offset = (page - 1) * per_page

    view = _resolve_view(request)
    view_kind = (view or {}).get("kind")
    is_fda = view_kind == "fda_match"
    is_tag = view_kind == "tag_match"
    view_match_info: dict[str, tuple[str, str]] = {}

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
