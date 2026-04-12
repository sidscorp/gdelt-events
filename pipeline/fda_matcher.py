"""FDA medical device company matcher.

Uses Aho-Corasick multi-pattern string matching to find articles mentioning
any FDA-registered medical device manufacturer in either the GKG
V2ENHANCEDORGANIZATIONS field, the extracted page title, or the GAL title.

Aho-Corasick scales to 20K+ patterns with near-linear throughput (~56K
rows/sec for GKG with typical org-field sizes on rainbow-boi, ~2 min for a
full 6.8M-row scan). Regex alternation with word boundaries was 120x slower
in benchmarks — the word-boundary check defeats RE2's automaton optimization.

Matches are materialized into the `fda_match_cache` table at ingest time.
Dashboard view queries do a fast IN-clause lookup, sub-100ms.
"""

import csv
import logging
import re
import time
from pathlib import Path

import ahocorasick
import duckdb

from .config import DB_PATH
from .loader import _open_connection

log = logging.getLogger(__name__)

CHUNK_SIZE = 50_000
_WORD_RE = re.compile(r"\w")

_TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.DOTALL)


MIN_PRODUCT_COUNT = 3
MIN_NAME_LEN = 6

STOP_WORDS: frozenset[str] = frozenset({
    "avanti", "proprio", "grandeur", "vesta",
    "eigen", "vorzeigen",
    "sonrisa", "lider",
    "path", "dome", "aztec", "klobe", "kusmo", "dekko",
    "jetyo", "molo", "fitz", "izon", "reev", "stae", "luci",
    "keos", "coag", "jlab", "brdh", "itek",
    "apple inc.", "apple inc", "tesla inc", "tesla inc.",
    "walmart inc.", "walmart inc",
})

STRIPPED_STOP_WORDS: frozenset[str] = frozenset({
    "apple", "tesla", "walmart", "google", "amazon", "microsoft",
    "meta", "facebook", "netflix", "disney", "target", "costco",
    "ford", "alphabet", "oracle", "intel", "nvidia", "samsung",
    "abbott", "smith", "johnson", "baxter", "miller", "davis",
    "bayer", "oxford", "stanford", "harvard", "york", "phoenix",
    "murray", "pearl", "pascal",
    "vision", "health", "medical", "dental", "pharma", "bio",
    "biotech", "life", "lifesciences", "genetics", "diagnostic",
    "therapeutics", "sciences", "science", "devices", "surgical",
    "waters",
    "american", "international", "global", "national", "united",
    "pacific", "atlantic", "european", "western", "eastern",
    "aswan",
    "beyond", "venture", "ventures", "planet", "outlook", "improve",
    "lemon", "compass", "meridian", "pivot", "graphy", "aurum",
    "apostle", "elmed", "billions", "micron", "mizuho", "a plus",
})


# Corporate suffixes to strip from legal firm names to produce a base-name
# variant. Applied iteratively until no more suffixes are removed. Order
# matters: longer variants first so "INCORPORATED" is tried before "INC".
_CORPORATE_SUFFIXES = [
    "incorporated",
    "corporation",
    "companies",
    "company",
    "limited",
    "holdings",
    "holding",
    "international",
    "l.l.c.",
    "l.l.c",
    "llc",
    "ltd.",
    "ltd",
    "inc.",
    "inc",
    "corp.",
    "corp",
    "co.",
    "co",
    "plc",
    "gmbh",
    "ag",
    "s.a.",
    "sa",
    "s.p.a.",
    "spa",
    "n.v.",
    "nv",
    "pte",
    "group",
]


def _strip_corporate_suffix(name: str) -> str | None:
    """Strip trailing corporate suffixes → base name, or None if unchanged."""
    if not name:
        return None
    n = name.strip().lower()
    changed = True
    while changed:
        changed = False
        n = n.rstrip(" .,;").strip()
        if not n:
            return None
        for suf in _CORPORATE_SUFFIXES:
            if n.endswith(" " + suf) or n.endswith("," + suf) or n.endswith(", " + suf):
                n = n[: -len(suf)].rstrip(" .,;")
                changed = True
                break
    n = n.strip()
    if not n or n == name.strip().lower():
        return None
    return n


def _has_class_2_or_3(device_classes: str | None) -> bool:
    """True if the company has at least one class-2 or class-3 device."""
    if not device_classes:
        return False
    parts = {p.strip() for p in device_classes.split("|")}
    return "2" in parts or "3" in parts


def _clean_name(
    name: str,
    product_count: int | None = None,
    specialty: str | None = None,
    device_classes: str | None = None,
) -> str | None:
    """Return cleaned name or None if the company shouldn't be matched."""
    if not name:
        return None
    n = name.strip()
    if len(n) < MIN_NAME_LEN:
        return None
    if n.lower() in STOP_WORDS:
        return None
    stripped = n.replace(".", "").replace("-", "").replace(" ", "").replace(",", "")
    if stripped.isdigit():
        return None
    if (product_count or 0) < MIN_PRODUCT_COUNT:
        return None
    s = (specialty or "").strip().lower()
    if not s or s == "unknown":
        return None
    if not _has_class_2_or_3(device_classes):
        return None
    return n


def import_companies(csv_path: Path, db_path: Path | None = None) -> int:
    """Load fda_companies.csv into the DuckDB `fda_companies` table, applying
    the quality filter."""
    db_path = db_path or DB_PATH
    con = _open_connection(db_path)
    try:
        con.execute("DELETE FROM fda_companies")
        rows = []
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    pc = int(row.get("product_count") or 0) or None
                except ValueError:
                    pc = None
                specialty = row.get("medical_specialties") or None
                device_classes = row.get("device_classes") or None
                name = _clean_name(
                    row.get("firm_name", ""), pc, specialty, device_classes
                )
                if not name:
                    continue
                try:
                    oon = int(row.get("owner_operator_number") or 0) or None
                except ValueError:
                    oon = None
                try:
                    sc = int(row.get("site_count") or 0) or None
                except ValueError:
                    sc = None
                rows.append((
                    oon, name, sc, pc,
                    row.get("device_classes") or None,
                    row.get("medical_specialties") or None,
                ))
        con.executemany(
            "INSERT INTO fda_companies "
            "(owner_operator_number, firm_name, site_count, product_count, "
            " device_classes, medical_specialties) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        con.execute("CHECKPOINT")
        return len(rows)
    finally:
        con.close()


def build_automaton(
    con: duckdb.DuckDBPyConnection,
    min_length: int = 2,
    include_stripped: bool = True,
) -> tuple[ahocorasick.Automaton, dict[str, str]]:
    """Build Aho-Corasick automaton from fda_companies. Returns (automaton, specialty_map).
    include_stripped adds base-name variants (e.g. 'pfizer' alongside 'pfizer inc.')."""
    rows = con.execute(
        "SELECT firm_name, medical_specialties FROM fda_companies "
        "WHERE firm_name IS NOT NULL"
    ).fetchall()
    A = ahocorasick.Automaton()
    seen: set[str] = set()
    specialty_map: dict[str, str] = {}
    # Value = (pattern, firm_name, match_type). _scan uses len(pattern)
    # for word-boundary math — must not use len(firm_name) which differs
    # for stripped patterns.
    for firm_name, specialty in rows:
        if len(firm_name) < min_length:
            continue
        low = firm_name.lower()
        if low not in seen:
            seen.add(low)
            A.add_word(low, (low, firm_name, "legal"))
        specialty_map[firm_name] = specialty or ""

        if include_stripped:
            base = _strip_corporate_suffix(firm_name)
            if (
                base
                and len(base) >= min_length
                and base not in seen
                and base not in STRIPPED_STOP_WORDS
                and not base.isdigit()
            ):
                seen.add(base)
                A.add_word(base, (base, firm_name, "stripped"))
    A.make_automaton()
    log.info(
        "automaton built: %d unique patterns (from %d firms, include_stripped=%s, min_length=%d)",
        len(seen), len(specialty_map), include_stripped, min_length,
    )
    return A, specialty_map


def _scan(
    automaton: ahocorasick.Automaton, text: str
) -> tuple[str, str, str] | None:
    """First word-bounded match → (firm_name, pattern, match_type) or None."""
    if not text:
        return None
    low = text.lower()
    n = len(low)
    for end_idx, value in automaton.iter(low):
        pattern, firm_name, match_type = value
        start = end_idx - len(pattern) + 1
        if start > 0 and _WORD_RE.match(low[start - 1]):
            continue
        if end_idx + 1 < n and _WORD_RE.match(low[end_idx + 1]):
            continue
        return firm_name, pattern, match_type
    return None


def _extract_title(extras_xml: str) -> str:
    if not extras_xml:
        return ""
    m = _TITLE_RE.search(extras_xml)
    return m.group(1) if m else ""


def _get_last_ts(con: duckdb.DuckDBPyConnection, source: str) -> int:
    row = con.execute(
        "SELECT last_crawled_at FROM fda_match_state WHERE source_type = ?",
        [source],
    ).fetchone()
    return row[0] if row else 0


def _set_last_ts(con: duckdb.DuckDBPyConnection, source: str, ts: int) -> None:
    con.execute(
        "INSERT OR REPLACE INTO fda_match_state (source_type, last_crawled_at, updated_at) "
        "VALUES (?, ?, current_timestamp)",
        [source, ts],
    )


def _bulk_insert_matches(con, matches: list[tuple]) -> None:
    """Insert a batch of 8-tuples:
    (article_id, source_type, matched_in, matched_name,
     medical_specialties, crawled_at, match_type, matched_pattern)
    """
    if not matches:
        return
    con.executemany(
        "INSERT INTO fda_match_cache "
        "(article_id, source_type, matched_in, matched_name, "
        " medical_specialties, crawled_at, match_type, matched_pattern) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        matches,
    )


def _match_gkg_stream(
    con: duckdb.DuckDBPyConnection,
    automaton: ahocorasick.Automaton,
    specialty_map: dict[str, str],
    since_ts: int = 0,
) -> tuple[int, int, int]:
    """Scan GKG rows newer than since_ts, write matches to cache.

    Quality-first: matches are only recorded when the FDA company name
    appears in the `V2ENHANCEDORGANIZATIONS` field. We don't scan the
    title for GKG — GDELT's NER (though imperfect) serves as a sanity
    check that the string actually refers to an organization rather
    than a random substring of natural-language text.

    Returns (rows_scanned, rows_matched, max_crawled_at).
    """
    where = 'WHERE "V1DATE" > ?' if since_ts else ""
    base_params: list = [since_ts] if since_ts else []

    scanned = 0
    inserted = 0
    max_ts = since_ts
    offset = 0

    while True:
        chunk = con.execute(
            f'SELECT "GKGRECORDID", "V1DATE", "V2ENHANCEDORGANIZATIONS" '
            f'FROM gkg {where} ORDER BY "V1DATE" LIMIT ? OFFSET ?',
            base_params + [CHUNK_SIZE, offset],
        ).fetchall()
        if not chunk:
            break

        matches: list[tuple] = []
        for gkg_id, v1date, orgs in chunk:
            scanned += 1
            if v1date and v1date > max_ts:
                max_ts = v1date
            if not orgs:
                continue
            hit = _scan(automaton, orgs)
            if hit:
                firm_name, pattern, match_type = hit
                matches.append((
                    gkg_id,
                    "gkg",
                    "orgs",
                    firm_name,
                    specialty_map.get(firm_name, ""),
                    v1date,
                    match_type,
                    pattern,
                ))

        if matches:
            _bulk_insert_matches(con, matches)
            inserted += len(matches)

        if scanned % 500_000 == 0:
            log.info("  GKG scanned %d, matched %d", scanned, inserted)

        if len(chunk) < CHUNK_SIZE:
            break
        offset += CHUNK_SIZE

    return scanned, inserted, max_ts


def _match_gal_stream(
    con: duckdb.DuckDBPyConnection,
    automaton: ahocorasick.Automaton,
    specialty_map: dict[str, str],
    since_ts: int = 0,
) -> tuple[int, int, int]:
    """Scan GAL rows newer than since_ts, write matches to cache.

    GAL has no orgs field, so we match the article title. The calling code
    builds the automaton with `min_length=5` so short/ambiguous names like
    "Keos" or "CorDx" won't match — there's no NER backstop for GAL.

    Also restricted to English articles: GAL includes Italian / Dutch /
    Spanish / German news where words like "avanti" / "eigen" / "proprio"
    appear in article titles and collide with legit FDA brand names.
    """
    base_conds = ["language = 'en'"]
    base_params: list = []
    if since_ts:
        base_conds.append("crawled_at > ?")
        base_params.append(since_ts)
    where = "WHERE " + " AND ".join(base_conds)

    scanned = 0
    inserted = 0
    max_ts = since_ts
    offset = 0

    while True:
        chunk = con.execute(
            f"SELECT url, crawled_at, title FROM gal {where} "
            f"ORDER BY crawled_at LIMIT ? OFFSET ?",
            base_params + [CHUNK_SIZE, offset],
        ).fetchall()
        if not chunk:
            break

        matches: list[tuple] = []
        for url, crawled_at, title in chunk:
            scanned += 1
            if crawled_at and crawled_at > max_ts:
                max_ts = crawled_at
            if not title:
                continue
            hit = _scan(automaton, title)
            if hit:
                firm_name, pattern, match_type = hit
                matches.append((
                    url,
                    "gal",
                    "title",
                    firm_name,
                    specialty_map.get(firm_name, ""),
                    crawled_at,
                    match_type,
                    pattern,
                ))

        if matches:
            _bulk_insert_matches(con, matches)
            inserted += len(matches)

        if scanned % 500_000 == 0:
            log.info("  GAL scanned %d, matched %d", scanned, inserted)

        if len(chunk) < CHUNK_SIZE:
            break
        offset += CHUNK_SIZE

    return scanned, inserted, max_ts


def initial_match(db_path: Path | None = None) -> dict:
    """Rebuild the FDA match cache from scratch across all GKG + GAL."""
    db_path = db_path or DB_PATH
    con = _open_connection(db_path)
    con.execute("SET threads = 8")
    summary: dict = {}
    try:
        log.info("Clearing fda_match_cache...")
        con.execute("DELETE FROM fda_match_cache")

        log.info("Building Aho-Corasick automatons (full, and GAL length>=5)...")
        t0 = time.time()
        full_automaton, specialty_map = build_automaton(con, min_length=2)
        gal_automaton, _ = build_automaton(con, min_length=5)
        summary["automaton_patterns_gkg"] = len(specialty_map)
        summary["automaton_patterns_gal"] = sum(1 for n in specialty_map if len(n) >= 5)
        summary["automaton_build_s"] = round(time.time() - t0, 2)

        log.info("Scanning GKG (orgs field only)...")
        t0 = time.time()
        scanned, _matched, max_ts = _match_gkg_stream(
            con, full_automaton, specialty_map, since_ts=0
        )
        summary["gkg_scanned"] = scanned
        summary["gkg_elapsed_s"] = round(time.time() - t0, 1)
        _set_last_ts(con, "gkg", max_ts)
        summary["gkg_matches"] = con.execute(
            "SELECT count(*) FROM fda_match_cache WHERE source_type='gkg'"
        ).fetchone()[0]

        log.info("Scanning GAL (title field, strict name length)...")
        t0 = time.time()
        scanned, _matched, max_ts = _match_gal_stream(
            con, gal_automaton, specialty_map, since_ts=0
        )
        summary["gal_scanned"] = scanned
        summary["gal_elapsed_s"] = round(time.time() - t0, 1)
        _set_last_ts(con, "gal", max_ts)
        summary["gal_matches"] = con.execute(
            "SELECT count(*) FROM fda_match_cache WHERE source_type='gal'"
        ).fetchone()[0]

        con.execute("CHECKPOINT")
    finally:
        con.close()
    return summary


def match_new_rows(db_path: Path | None = None) -> dict:
    """Incremental match — scans only rows newer than the last recorded
    timestamp per source. Called from gdelt_ingest.py after each 15-min load.
    """
    db_path = db_path or DB_PATH
    con = _open_connection(db_path)
    con.execute("SET threads = 4")
    summary: dict = {}
    try:
        full_automaton, specialty_map = build_automaton(con, min_length=2)
        gal_automaton, _ = build_automaton(con, min_length=5)

        for src, streamer, auto in [
            ("gkg", _match_gkg_stream, full_automaton),
            ("gal", _match_gal_stream, gal_automaton),
        ]:
            since = _get_last_ts(con, src)
            t0 = time.time()
            scanned, _matched, max_ts = streamer(con, auto, specialty_map, since_ts=since)
            summary[f"{src}_scanned"] = scanned
            summary[f"{src}_elapsed_s"] = round(time.time() - t0, 2)
            if max_ts > since:
                _set_last_ts(con, src, max_ts)

        con.execute("CHECKPOINT")
    finally:
        con.close()
    return summary


def prune_cache(con: duckdb.DuckDBPyConnection, cutoff: int) -> int:
    """Delete fda_match_cache rows older than the cutoff. Called by pruner."""
    before = con.execute("SELECT count(*) FROM fda_match_cache").fetchone()[0]
    con.execute("DELETE FROM fda_match_cache WHERE crawled_at < ?", [cutoff])
    after = con.execute("SELECT count(*) FROM fda_match_cache").fetchone()[0]
    return before - after
