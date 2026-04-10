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
    """Human-readable relative time."""
    if not dt:
        return ""
    delta = datetime.now() - dt
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


@app.route("/api/articles")
def api_articles():
    """Main API endpoint for article list with filtering."""
    con = get_db()
    if con is None:
        return jsonify({"error": "Database is busy (backfill in progress). Try again shortly.", "articles": [], "total": 0, "page": 1, "per_page": 50, "pages": 0}), 503

    # Pagination
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 50, type=int))
    offset = (page - 1) * per_page

    # Build WHERE clauses
    conditions = []
    params = []

    # Time filter
    hours = request.args.get("hours", type=int)
    if hours:
        cutoff = datetime.now() - timedelta(hours=hours)
        cutoff_ts = int(cutoff.strftime("%Y%m%d%H%M%S"))
        conditions.append('"V1DATE" >= ?')
        params.append(cutoff_ts)

    date_from = request.args.get("date_from")
    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d")
            conditions.append('"V1DATE" >= ?')
            params.append(int(dt.strftime("%Y%m%d000000")))
        except ValueError:
            pass

    date_to = request.args.get("date_to")
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d")
            conditions.append('"V1DATE" <= ?')
            params.append(int(dt.strftime("%Y%m%d235959")))
        except ValueError:
            pass

    # Text search across persons, orgs, themes, source
    q = request.args.get("q", "").strip()
    if q:
        conditions.append(
            '("V2ENHANCEDPERSONS" ILIKE ? OR "V2ENHANCEDORGANIZATIONS" ILIKE ? '
            'OR "V2ENHANCEDTHEMES" ILIKE ? OR "V2SOURCECOMMONNAME" ILIKE ? '
            'OR "V2EXTRASXML" ILIKE ? OR "V2DOCUMENTIDENTIFIER" ILIKE ?)'
        )
        like = f"%{q}%"
        params.extend([like] * 6)

    # Specific filters
    person = request.args.get("person", "").strip()
    if person:
        conditions.append('"V2ENHANCEDPERSONS" ILIKE ?')
        params.append(f"%{person}%")

    org = request.args.get("org", "").strip()
    if org:
        conditions.append('"V2ENHANCEDORGANIZATIONS" ILIKE ?')
        params.append(f"%{org}%")

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

    # Tone filter
    tone_min = request.args.get("tone_min", type=float)
    tone_max = request.args.get("tone_max", type=float)
    if tone_min is not None or tone_max is not None:
        # V15TONE starts with the tone value followed by comma
        conditions.append('"V15TONE" IS NOT NULL')
        if tone_min is not None:
            conditions.append('CAST(split_part("V15TONE", \',\', 1) AS DOUBLE) >= ?')
            params.append(tone_min)
        if tone_max is not None:
            conditions.append('CAST(split_part("V15TONE", \',\', 1) AS DOUBLE) <= ?')
            params.append(tone_max)

    where = " AND ".join(conditions) if conditions else "1=1"

    # Count total
    count_sql = f'SELECT count(*) FROM gkg WHERE {where}'
    total = con.execute(count_sql, params).fetchone()[0]

    # Fetch articles
    sql = f"""
        SELECT "GKGRECORDID", "V1DATE", "V2SOURCECOMMONNAME", "V2DOCUMENTIDENTIFIER",
               "V2ENHANCEDTHEMES", "V2ENHANCEDLOCATIONS", "V2ENHANCEDPERSONS",
               "V2ENHANCEDORGANIZATIONS", "V15TONE", "V2EXTRASXML", "V2SHARINGIMAGE"
        FROM gkg
        WHERE {where}
        ORDER BY "V1DATE" DESC
        LIMIT ? OFFSET ?
    """
    rows = con.execute(sql, params + [per_page, offset]).fetchall()

    articles = []
    for row in rows:
        (gkg_id, v1date, source_name, url, themes, locations, persons,
         orgs, tone_str, extras_xml, sharing_image) = row

        dt = format_timestamp(v1date)
        tone = parse_tone(tone_str)
        title = extract_title(extras_xml) or ""

        articles.append({
            "id": gkg_id,
            "timestamp": dt.isoformat() if dt else None,
            "time_ago": time_ago(dt),
            "source": source_name or "",
            "url": url or "",
            "title": title,
            "persons": parse_enhanced_list(persons),
            "organizations": parse_enhanced_list(orgs),
            "themes": parse_enhanced_list(themes),
            "locations": parse_locations(locations),
            "tone": tone,
            "image": sharing_image or None,
        })

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

    return jsonify({
        "total_articles": row[0],
        "earliest": str(row[1]),
        "latest": str(row[2]),
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
    cutoff = datetime.now() - timedelta(hours=hours)
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
