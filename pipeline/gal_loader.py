"""Load GDELT GAL (JSON-NL gzipped) files into DuckDB."""

import gzip
import json
import logging
from pathlib import Path

import duckdb

from .config import DB_PATH
from .loader import BATCH_COMMIT_SIZE, _open_connection

log = logging.getLogger(__name__)


GAL_INSERT_SQL = """
    INSERT OR IGNORE INTO gal (
        url, crawled_at, published_at, domain, outlet_name, outlet_logo, outlet_twitter,
        title, image, description, language, author
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _parse_iso_date_to_int(iso_str: str) -> int | None:
    """Convert '2026-04-10T23:32:00.000Z' to YYYYMMDDHHMMSS int."""
    if not iso_str:
        return None
    try:
        # Strip fractional seconds and Z
        s = iso_str.replace("Z", "").split(".")[0]
        # s is now 'YYYY-MM-DDTHH:MM:SS'
        d, t = s.split("T")
        return int(d.replace("-", "") + t.replace(":", ""))
    except (ValueError, AttributeError):
        return None


def _extract_gal_filename_timestamp(path: Path) -> int | None:
    """Extract YYYYMMDDHHMMSS from a GAL filename like '20260410233200.gal.json.gz'."""
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


def load_gal_file(con: duckdb.DuckDBPyConnection, gal_path: Path) -> int:
    """Load a single .gal.json.gz file into the gal table.

    Returns the number of rows actually inserted (duplicates via INSERT OR IGNORE).
    """
    if is_gal_file_loaded(con, gal_path.name):
        return 0

    batch_ts = _extract_gal_filename_timestamp(gal_path)
    rows = []

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

                rows.append((
                    url,
                    batch_ts,  # crawled_at — when GDELT observed the article
                    _parse_iso_date_to_int(rec.get("date", "")),  # published_at
                    rec.get("domain") or None,
                    rec.get("outletName") or None,
                    rec.get("outletLogo") or None,
                    rec.get("outletTwitter") or None,
                    rec.get("title") or None,
                    rec.get("image") or None,
                    rec.get("desc") or None,
                    rec.get("lang") or None,
                    rec.get("author") or None,
                ))
    except (OSError, gzip.BadGzipFile) as e:
        log.warning("Failed to read %s: %s", gal_path.name, e)
        return 0

    if not rows:
        con.execute(
            "INSERT INTO _gal_ingest_log (filename, batch_timestamp, row_count) VALUES (?, ?, ?)",
            [gal_path.name, batch_ts or 0, 0],
        )
        return 0

    # Count before + after to know actual insertions (INSERT OR IGNORE drops dupes)
    before = con.execute("SELECT count(*) FROM gal").fetchone()[0]
    con.executemany(GAL_INSERT_SQL, rows)
    after = con.execute("SELECT count(*) FROM gal").fetchone()[0]
    inserted = after - before

    con.execute(
        "INSERT INTO _gal_ingest_log (filename, batch_timestamp, row_count) VALUES (?, ?, ?)",
        [gal_path.name, batch_ts or 0, inserted],
    )
    return inserted


def load_gal_batch(
    gal_files: list[Path],
    db_path: Path | None = None,
) -> dict:
    """Load multiple GAL files with periodic commits. Returns summary dict."""
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
            con.execute("CHECKPOINT")
            con.close()
        except Exception:
            pass

    return summary
