"""GDELT News Dashboard — Tufte-inspired breaking news viewer."""

import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
from flask import Flask, render_template, request, jsonify

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gdelt.duckdb"

app = Flask(__name__)


def get_db(max_retries=3):
    """Get a read-only DuckDB connection, retrying briefly on lock conflicts."""
    for attempt in range(max_retries):
        try:
            return duckdb.connect(str(DB_PATH), read_only=True)
        except duckdb.IOException:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
    return None


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
    """Human-readable relative time. GDELT timestamps are UTC."""
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
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


@app.route("/")
def index():
    return render_template("index.html")


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

    source = request.args.get("source", "").strip()
    if source:
        conditions.append('"V2SOURCECOMMONNAME" ILIKE ?')
        params.append(f"%{source}%")

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

    q = request.args.get("q", "").strip()
    if q:
        conditions.append("(title ILIKE ? OR description ILIKE ? OR domain ILIKE ? OR outlet_name ILIKE ?)")
        params.extend([f"%{q}%"] * 4)

    title = request.args.get("title", "").strip()
    if title:
        conditions.append("title ILIKE ?")
        params.append(f"%{title}%")

    source = request.args.get("source", "").strip()
    if source:
        conditions.append("domain ILIKE ?")
        params.append(f"%{source}%")

    where = " AND ".join(conditions) if conditions else "1=1"
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
    (url, crawled_at, domain, outlet_name, title, image, description) = row
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

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 50, type=int))
    offset = (page - 1) * per_page

    gkg_only = _gkg_entity_filters_set(request)
    gkg_where, gkg_params = _build_gkg_where(request)

    gkg_cols = (
        '"GKGRECORDID", "V1DATE", "V2SOURCECOMMONNAME", "V2DOCUMENTIDENTIFIER", '
        '"V2ENHANCEDTHEMES", "V2ENHANCEDLOCATIONS", "V2ENHANCEDPERSONS", '
        '"V2ENHANCEDORGANIZATIONS", "V15TONE", "V2EXTRASXML", "V2SHARINGIMAGE"'
    )

    if gkg_only:
        # Entity-filtered query: GKG only
        total = con.execute(f"SELECT count(*) FROM gkg WHERE {gkg_where}", gkg_params).fetchone()[0]
        rows = con.execute(
            f'SELECT {gkg_cols} FROM gkg WHERE {gkg_where} '
            f'ORDER BY "V1DATE" DESC LIMIT ? OFFSET ?',
            gkg_params + [per_page, offset],
        ).fetchall()
        articles = [_gkg_row_to_article(r) for r in rows]
    else:
        # Merged GKG + GAL query. Over-fetch from each source so post-dedup
        # we still have enough for the requested page.
        fetch_n = (page * per_page) + per_page  # generous cushion

        gkg_rows = con.execute(
            f'SELECT {gkg_cols} FROM gkg WHERE {gkg_where} '
            f'ORDER BY "V1DATE" DESC LIMIT ?',
            gkg_params + [fetch_n],
        ).fetchall()

        gal_where, gal_params = _build_gal_where(request)
        try:
            gal_rows = con.execute(
                'SELECT url, crawled_at, domain, outlet_name, title, image, description '
                f'FROM gal WHERE {gal_where} '
                'ORDER BY crawled_at DESC LIMIT ?',
                gal_params + [fetch_n],
            ).fetchall()
        except Exception:
            gal_rows = []  # GAL table may not exist yet

        gkg_total = con.execute(f"SELECT count(*) FROM gkg WHERE {gkg_where}", gkg_params).fetchone()[0]
        try:
            gal_total = con.execute(f"SELECT count(*) FROM gal WHERE {gal_where}", gal_params).fetchone()[0]
        except Exception:
            gal_total = 0

        # Merge + dedup by URL, prefer GKG
        seen_urls = set()
        merged = []
        for r in gkg_rows:
            art = _gkg_row_to_article(r)
            if art["url"] and art["url"] not in seen_urls:
                seen_urls.add(art["url"])
                merged.append(art)
        for r in gal_rows:
            art = _gal_row_to_article(r)
            if art["url"] and art["url"] not in seen_urls:
                seen_urls.add(art["url"])
                merged.append(art)

        merged.sort(key=lambda a: a["sort_key"], reverse=True)

        # Approximate total: sum of the two filtered counts minus estimated
        # overlap. We can't know exact overlap without a JOIN, so we report
        # the sum — close enough for the UI's "N articles" stat.
        total = gkg_total + gal_total

        articles = merged[offset:offset + per_page]

    for a in articles:
        a.pop("sort_key", None)

    con.close()

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
    row = con.execute("""
        SELECT count(*),
               min("V1DATE"),
               max("V1DATE"),
               count(DISTINCT "V2SOURCECOMMONNAME")
        FROM gkg
    """).fetchone()
    con.close()

    latest_dt = format_timestamp(row[2])
    earliest_dt = format_timestamp(row[1])

    return jsonify({
        "total_articles": row[0],
        "earliest": str(row[1]),
        "latest": str(row[2]),
        "earliest_date": earliest_dt.strftime("%Y-%m-%d") if earliest_dt else None,
        "latest_date": latest_dt.strftime("%Y-%m-%d") if latest_dt else None,
        "earliest_display": earliest_dt.strftime("%b %d, %Y") if earliest_dt else None,
        "latest_display": latest_dt.strftime("%b %d, %Y %H:%M UTC") if latest_dt else None,
        "latest_ago": time_ago(latest_dt),
        "sources": row[3],
    })


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8015, debug=True)
