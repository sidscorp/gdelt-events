"""Load GDELT GAL (JSON-NL gzipped) files into DuckDB.

Uses pandas DataFrame insertion for speed — DuckDB's `INSERT INTO ... SELECT
* FROM df` is vectorized and ~100x faster than `executemany` for our workload.
Dedup is NOT enforced at insert time (the gal table has no PRIMARY KEY) — we
use post-load DELETE via window function for dedup. This trades ingestion
speed for a cleanup step at the end.
"""

import gzip
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from .config import DB_PATH
from .loader import BATCH_COMMIT_SIZE, _open_connection

log = logging.getLogger(__name__)

# gal_recent: rolling ~8-day mirror of gal that the dashboard's window queries
# use (routing threshold there is 7 days — see dashboard/articles.py RECENT_HOURS;
# the extra day keeps the slice a strict superset). Built once by
# pipeline/build_gal_recent.py; mirrored inserts + pruning happen here.
GAL_RECENT_KEEP_DAYS = 8
_gal_recent_exists = None


def _has_gal_recent(con) -> bool:
    global _gal_recent_exists
    if _gal_recent_exists is None:
        try:
            _gal_recent_exists = bool(con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'gal_recent'"
            ).fetchone())
        except Exception:
            _gal_recent_exists = False
    return _gal_recent_exists


GAL_COLUMNS = [
    "url", "crawled_at", "published_at", "domain", "outlet_name",
    "outlet_logo", "outlet_twitter", "title", "image", "description",
    "language", "author",
]


def _parse_iso_date_to_int(iso_str: str) -> int | None:
    if not iso_str:
        return None
    try:
        s = iso_str.replace("Z", "").split(".")[0]
        d, t = s.split("T")
        return int(d.replace("-", "") + t.replace(":", ""))
    except (ValueError, AttributeError):
        return None


def _extract_gal_filename_timestamp(path: Path) -> int | None:
    stem = path.name.split(".")[0]
    try:
        return int(stem)
    except ValueError:
        return None


def is_gal_file_loaded(con: duckdb.DuckDBPyConnection, filename: str) -> bool:
    result = con.execute(
        "SELECT 1 FROM _gal_ingest_log WHERE filename = ?", [filename]
    ).fetchone()
    return result is not None


def _read_gal_file_to_records(gal_path: Path, batch_ts: int | None) -> list[dict]:
    """Parse a gzipped JSON-NL GAL file into a list of dicts."""
    records = []
    try:
        with gzip.open(gal_path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                url = rec.get("url")
                if not url:
                    continue
                records.append({
                    "url": url,
                    "crawled_at": batch_ts,
                    "published_at": _parse_iso_date_to_int(rec.get("date", "")),
                    "domain": rec.get("domain") or None,
                    "outlet_name": rec.get("outletName") or None,
                    "outlet_logo": rec.get("outletLogo") or None,
                    "outlet_twitter": rec.get("outletTwitter") or None,
                    "title": rec.get("title") or None,
                    "image": rec.get("image") or None,
                    "description": rec.get("desc") or None,
                    "language": rec.get("lang") or None,
                    "author": rec.get("author") or None,
                })
    except (OSError, gzip.BadGzipFile) as e:
        log.warning("Failed to read %s: %s", gal_path.name, e)
    return records


def load_gal_file(con: duckdb.DuckDBPyConnection, gal_path: Path) -> int:
    """Load a single GAL file. Returns rows inserted (no dedup at insert time)."""
    if is_gal_file_loaded(con, gal_path.name):
        return 0

    batch_ts = _extract_gal_filename_timestamp(gal_path)
    records = _read_gal_file_to_records(gal_path, batch_ts)
    count = len(records)

    if count > 0:
        df = pd.DataFrame(records, columns=GAL_COLUMNS)
        # Register the DataFrame as a temp view and bulk insert
        con.register("gal_batch", df)
        con.execute(
            "INSERT INTO gal (url, crawled_at, published_at, domain, "
            "outlet_name, outlet_logo, outlet_twitter, title, image, "
            "description, language, author) "
            "SELECT url, crawled_at, published_at, domain, "
            "outlet_name, outlet_logo, outlet_twitter, title, image, "
            "description, language, author FROM gal_batch"
        )
        if _has_gal_recent(con):
            con.execute(
                "INSERT INTO gal_recent (url, crawled_at, published_at, domain, "
                "outlet_name, outlet_logo, outlet_twitter, title, image, "
                "description, language, author) "
                "SELECT url, crawled_at, published_at, domain, "
                "outlet_name, outlet_logo, outlet_twitter, title, image, "
                "description, language, author FROM gal_batch"
            )
        con.unregister("gal_batch")

    con.execute(
        "INSERT INTO _gal_ingest_log (filename, batch_timestamp, row_count) VALUES (?, ?, ?)",
        [gal_path.name, batch_ts or 0, count],
    )
    return count


def load_gal_batch(
    gal_files: list[Path],
    db_path: Path | None = None,
) -> dict:
    """Load multiple GAL files with periodic commits."""
    db_path = db_path or DB_PATH
    summary = {"gal": 0, "files": 0, "skipped": 0, "errors": 0}
    total = len(gal_files)
    if total == 0:
        return summary

    con = _open_connection(db_path)
    sorted_files = sorted(gal_files)

    try:
        for i, gal_path in enumerate(sorted_files, start=1):
            try:
                if is_gal_file_loaded(con, gal_path.name):
                    summary["skipped"] += 1
                    continue
                inserted = load_gal_file(con, gal_path)
                summary["gal"] += inserted
                summary["files"] += 1
            except Exception:
                log.exception("Error loading GAL file %s", gal_path.name)
                summary["errors"] += 1

            if i % BATCH_COMMIT_SIZE == 0:
                con.execute("CHECKPOINT")
                con.close()
                log.info(
                    "GAL progress: %d/%d loaded (%d rows, %d skipped, %d errors)",
                    i, total, summary["gal"], summary["skipped"], summary["errors"],
                )
                con = _open_connection(db_path)
    finally:
        try:
            if summary["gal"] and _has_gal_recent(con):
                cutoff = int((datetime.utcnow() - timedelta(days=GAL_RECENT_KEEP_DAYS))
                             .strftime("%Y%m%d%H%M%S"))
                con.execute("DELETE FROM gal_recent WHERE crawled_at < ?", [cutoff])
        except Exception:
            log.exception("gal_recent prune failed (non-fatal)")
        try:
            con.execute("CHECKPOINT")
            con.close()
        except Exception:
            pass

    return summary


def ensure_gal_indexes(db_path: Path | None = None) -> None:
    """Create the GAL indexes if they don't exist. Run after a bulk load.

    Index creation on a large table takes seconds; maintaining them per-INSERT
    during a bulk load takes hours. So the schema intentionally omits them and
    the loader is responsible for recreating them at the end of a run.
    """
    db_path = db_path or DB_PATH
    con = _open_connection(db_path)
    try:
        for col, name in [
            ("crawled_at", "idx_gal_crawled"),
            ("domain", "idx_gal_domain"),
            ("url", "idx_gal_url"),
        ]:
            log.info("Creating %s ...", name)
            con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON gal({col})")
        con.execute("CHECKPOINT")
    finally:
        con.close()


def dedupe_gal(db_path: Path | None = None) -> int:
    """Remove duplicate GAL rows (keep first by crawled_at per URL).

    Called after backfills to clean up the INSERT-all-without-PK approach.
    Returns the number of rows deleted.
    """
    db_path = db_path or DB_PATH
    con = _open_connection(db_path)
    try:
        before = con.execute("SELECT count(*) FROM gal").fetchone()[0]
        # DuckDB supports QUALIFY with window functions
        con.execute("""
            CREATE OR REPLACE TABLE gal AS
            SELECT url, crawled_at, published_at, domain, outlet_name, outlet_logo,
                   outlet_twitter, title, image, description, language, author, loaded_at
            FROM gal
            QUALIFY row_number() OVER (PARTITION BY url ORDER BY crawled_at) = 1
        """)
        # Recreate indexes that were dropped with the table
        con.execute("CREATE INDEX IF NOT EXISTS idx_gal_crawled ON gal(crawled_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_gal_domain ON gal(domain)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_gal_url ON gal(url)")
        after = con.execute("SELECT count(*) FROM gal").fetchone()[0]
        con.execute("CHECKPOINT")
        return before - after
    finally:
        con.close()
