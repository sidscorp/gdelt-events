"""
GDELT DOC API client — reusable functions for searching GDELT articles.

No display/CLI logic here. Returns raw data for callers to present however they want.
"""

import time
import requests
from datetime import datetime

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"


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


def search_gdelt(query, days=7, limit=25):
    """Execute search against GDELT DOC API.

    Returns a list of article dicts, or an empty list on failure.
    """
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": min(limit, 250),
        "timespan": f"{days}d",
        "format": "json",
        "sort": "hybridrel",
    }

    for attempt in range(3):
        resp = requests.get(GDELT_DOC_API, params=params)
        if resp.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        try:
            data = resp.json()
        except requests.exceptions.JSONDecodeError:
            return []
        return data.get("articles", [])

    return []


def parse_date(seendate):
    """Parse GDELT's seendate format (20260224T163000Z) to datetime."""
    if not seendate:
        return None
    try:
        return datetime.strptime(seendate, "%Y%m%dT%H%M%SZ")
    except (ValueError, TypeError):
        return None
