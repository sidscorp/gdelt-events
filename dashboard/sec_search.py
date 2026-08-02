"""Find the company someone meant.

A ticker box that only does exact matches is a dead end for anyone who does not
already know the ticker - which is most people. This is a ranked cascade over the
local store, no model and no network:

    exact ticker  ->  prefix ticker  ->  FTS5 name match  ->  loose substring

Ties break on company size (latest revenue), because "apple" should return Apple
Inc. and not a shell company with Apple in its name. Multi-class tickers come
from the submissions ingest, so GOOG resolves as well as GOOGL.
"""
from __future__ import annotations

import difflib
import re
import sqlite3

# FTS5 treats these as syntax; a user typing "AT&T" or "3M Co." should not get a
# query error.
_FTS_UNSAFE = re.compile(r'[^\w\s]')


def _rows(con: sqlite3.Connection, sql: str, *params) -> list[sqlite3.Row]:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _size_sql(alias: str = "c") -> str:
    """Latest revenue for a company, as a ranking signal."""
    return (f"(SELECT max(s.revenue) FROM snapshots s "
            f"WHERE s.cik = {alias}.cik AND s.revenue IS NOT NULL)")


def search(con: sqlite3.Connection, term: str, limit: int = 8) -> list[dict]:
    """Best matches for a free-text term, most likely first."""
    term = (term or "").strip()
    if not term:
        return []
    up = term.upper()
    seen: dict[int, dict] = {}

    def add(rows, why: str, base: float):
        for r in rows:
            d = dict(r)
            if d["cik"] in seen:
                continue
            d["match"] = why
            d["score"] = base + min((d.get("size") or 0) / 1e12, 0.9)
            seen[d["cik"]] = d

    size = _size_sql()
    # 1. exact ticker, any share class
    add(_rows(con, f"""SELECT c.cik, c.name, t.ticker, c.sic_description, {size} AS size
                       FROM tickers t JOIN companies c ON c.cik = t.cik
                       WHERE t.ticker = ? ORDER BY t.is_primary DESC""", up),
        "ticker", 100)
    # 2. ticker prefix - "goog" should reach GOOGL
    if len(seen) < limit:
        add(_rows(con, f"""SELECT c.cik, c.name, t.ticker, c.sic_description, {size} AS size
                           FROM tickers t JOIN companies c ON c.cik = t.cik
                           WHERE t.ticker LIKE ? ORDER BY length(t.ticker) LIMIT 20""",
                  up + "%"), "ticker-prefix", 80)
    # 3. name, via FTS5
    if len(seen) < limit:
        clean = _FTS_UNSAFE.sub(" ", term).strip()
        if clean:
            fts = " ".join(f'"{w}"*' for w in clean.split())
            add(_rows(con, f"""SELECT c.cik, c.name, c.ticker, c.sic_description, {size} AS size
                               FROM companies_fts f JOIN companies c ON c.cik = f.cik
                               WHERE companies_fts MATCH ? LIMIT 40""", fts),
                "name", 60)
    # 4. last resort: substring, for names FTS tokenisation splits differently
    if len(seen) < limit:
        add(_rows(con, f"""SELECT c.cik, c.name, c.ticker, c.sic_description, {size} AS size
                           FROM companies c WHERE upper(c.name) LIKE ? LIMIT 40""",
                  f"%{up}%"), "name-loose", 40)

    # 5. squashed match: "jp morgan" should reach JPMORGAN CHASE, and "at and t"
    #    should reach AT&T. Punctuation and spacing are how people actually type.
    if len(seen) < limit:
        squashed = re.sub(r"[^A-Z0-9]", "", up)
        if len(squashed) >= 3:
            add(_rows(con, f"""SELECT c.cik, c.name, c.ticker, c.sic_description, {size} AS size
                               FROM companies c
                               WHERE replace(replace(replace(replace(upper(c.name),' ',''),
                                     '.',''),',',''),'&','') LIKE ? LIMIT 20""",
                      f"%{squashed}%"), "name-squashed", 45)

    # 6. typo tolerance. Only runs when everything above found nothing, because it
    #    scans every name - "microsft" has no prefix or token in common with
    #    "MICROSOFT CORP", so no index can help.
    if not seen and len(up) >= 4:
        names = _rows(con, f"""SELECT c.cik, c.name, c.ticker, c.sic_description, {size} AS size
                               FROM companies c WHERE {size} IS NOT NULL""")
        lookup = {}
        for r in names:
            first = (r["name"] or "").upper().split()[0] if r["name"] else ""
            if first:
                lookup.setdefault(first, []).append(r)
        close = difflib.get_close_matches(up, list(lookup), n=3, cutoff=0.72)
        for word in close:
            add(lookup[word][:3], "fuzzy", 30)

    out = sorted(seen.values(), key=lambda d: d["score"], reverse=True)
    # A company with no financial periods is noise in a financials search.
    withdata = [d for d in out if d.get("size") is not None]
    return (withdata or out)[:limit]


def resolve(con: sqlite3.Connection, term: str) -> tuple[dict | None, list[dict]]:
    """(best match, alternatives). Empty best match means nothing plausible."""
    hits = search(con, term)
    if not hits:
        return None, []
    return hits[0], hits[1:5]
