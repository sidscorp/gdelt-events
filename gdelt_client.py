"""
GDELT DOC API client — reusable functions for searching GDELT articles.

No display/CLI logic here. Returns raw data for callers to present however they want.
"""

import re
import time
import requests
from datetime import datetime

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# Timespan unit aliases -> GDELT format suffix
_TIMESPAN_ALIASES = {
    "d": "d", "day": "d", "days": "d",
    "w": "w", "wk": "w", "week": "w", "weeks": "w",
    "m": "m", "mo": "m", "month": "m", "months": "m",
    "h": "h", "hr": "h", "hour": "h", "hours": "h",
    "min": "min", "mins": "min", "minutes": "min",
}

SORT_OPTIONS = {
    "relevance": "hybridrel",
    "date": "datedesc",
    "date-asc": "dateasc",
    "tone-asc": "toneasc",
    "tone-desc": "tonedesc",
}

_BOOLEAN_WORDS = re.compile(r"\b(and|or|not)\b", flags=re.IGNORECASE)


def parse_timespan(timespan):
    """Convert flexible timespan string to GDELT API format.

    Accepts: '7d', '2w', '1m', '3m', '24h', '15min', or bare int (treated as days).
    Returns: GDELT-compatible timespan string like '7d', '2w', '1m', '24h', '15min'.
    """
    timespan = str(timespan).strip().lower()

    # Bare integer -> days
    if timespan.isdigit():
        return f"{timespan}d"

    # Match number + unit
    match = re.fullmatch(r"(\d+)\s*([a-z]+)", timespan)
    if not match:
        return timespan  # pass through as-is, let GDELT validate

    value, unit = match.group(1), match.group(2)
    suffix = _TIMESPAN_ALIASES.get(unit)

    if suffix is None:
        return timespan  # unrecognized unit, pass through

    return f"{value}{suffix}"


def normalize_query(query):
    """Normalize boolean operators and wrap bare OR expressions in parens.

    Transforms:
    - 'and'/'or'/'not' -> 'AND'/'OR'/'NOT'
    - Strips redundant parens around non-OR expressions
    - Bare top-level OR wrapped in parens: 'a or b' -> '(a OR b)'
    """
    # Uppercase boolean operators
    normalized = _BOOLEAN_WORDS.sub(lambda m: m.group(0).upper(), query)

    # Strip redundant parens around non-OR expressions
    previous = None
    current = normalized
    while previous != current:
        previous = current
        current = re.sub(
            r'\(([^()]*?)\)',
            lambda m: m.group(1) if " OR " not in m.group(1) else m.group(0),
            current,
        )
    normalized = current

    # Wrap bare top-level OR in parens
    if " OR " in normalized:
        if not re.search(r"\([^)]*\bOR\b[^)]*\)", normalized):
            normalized = f"({normalized})"

    return normalized.strip()


def interpret_tone(avg_tone):
    """Interpret GDELT tone value into a human-readable label and Rich style.

    Returns: (label, style) tuple.
    """
    if avg_tone > 2:
        return "POSITIVE", "bold green"
    elif avg_tone > 0:
        return "SLIGHTLY POSITIVE", "yellow"
    elif avg_tone > -2:
        return "SLIGHTLY NEGATIVE", "yellow"
    else:
        return "NEGATIVE", "bold red"


def build_query(search_terms, country=None, source_country=None, domain=None):
    """Build the GDELT DOC API query string.

    All filters are embedded in the query parameter per GDELT API spec.
    Spaces between words = implicit AND.
    """
    parts = [search_terms, "sourcelang:english"]

    # Location context: adds country names to the search (articles must mention these places)
    if country:
        countries = [c.strip() for c in country.split(",") if c.strip()]
        if len(countries) == 1:
            parts.append(f'"{countries[0]}"')
        else:
            country_query = " OR ".join([f'"{c}"' for c in countries])
            parts.append(f"({country_query})")

    # Source country: filters by where the news outlet is based (FIPS code or name)
    if source_country:
        parts.append(f"sourcecountry:{source_country.replace(' ', '').lower()}")

    # Domain filter: limit to specific news domain
    if domain:
        parts.append(f"domainis:{domain}")

    return " ".join(parts)


def search_gdelt(query, timespan="7d", limit=25, sort="hybridrel", mode="artlist"):
    """Execute search against GDELT DOC API.

    Returns the full JSON response dict, or None on failure.
    """
    params = {
        "query": query,
        "mode": mode,
        "maxrecords": min(limit, 250),
        "timespan": parse_timespan(timespan),
        "format": "json",
        "sort": sort,
    }

    for attempt in range(3):
        try:
            resp = requests.get(GDELT_DOC_API, params=params, timeout=30)
        except requests.exceptions.RequestException:
            return None

        if resp.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            return None

        # Check content-type before parsing JSON
        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            return None

        try:
            return resp.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            return None

    return None


def parse_date(seendate):
    """Parse GDELT's seendate format (20260224T163000Z) to datetime."""
    if not seendate:
        return None
    try:
        return datetime.strptime(seendate, "%Y%m%dT%H%M%SZ")
    except (ValueError, TypeError):
        return None
