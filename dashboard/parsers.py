"""Pure parsing/formatting helpers for GDELT fields.

These are stateless string/timestamp transformers shared by the article feed,
stats, and entity routes. Kept dependency-free (only stdlib) so any module can
import them without pulling in Flask or the app.
"""

import re
from datetime import datetime


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
