"""Retention cleanup for GDELT data — removes old zips and DuckDB rows."""

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from .config import DB_PATH, GAL_RAW_DIR, RAW_DIR, RETENTION_DAYS
from .downloader import FILENAME_RE
from .gal_downloader import GAL_FILENAME_RE

log = logging.getLogger(__name__)


def _cutoff_timestamp(retention_days: int | None = None) -> int:
    """Calculate the cutoff timestamp as a YYYYMMDDHHMMSS integer."""
    days = retention_days if retention_days is not None else RETENTION_DAYS
    cutoff = datetime.now(tz=None) - timedelta(days=days)
    return int(cutoff.strftime("%Y%m%d%H%M%S"))


def prune_database(con: duckdb.DuckDBPyConnection, retention_days: int | None = None) -> dict:
    """Delete rows older than the retention window. Returns {table: rows_deleted}."""
    cutoff = _cutoff_timestamp(retention_days)
    deleted = {}

    for table, col in [
        ("events", "DATEADDED"),
        ("mentions", "MentionTimeDate"),
        ("gkg", "V1DATE"),
        ("gal", "crawled_at"),
    ]:
        before = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        con.execute(f"DELETE FROM {table} WHERE \"{col}\" < ?", [cutoff])
        after = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        deleted[table] = before - after

    # Clean ingest logs
    con.execute("DELETE FROM _ingest_log WHERE batch_timestamp < ?", [cutoff])
    con.execute("DELETE FROM _gal_ingest_log WHERE batch_timestamp < ?", [cutoff])

    log.info("Pruned rows (cutoff %d): %s", cutoff, deleted)
    return deleted


def prune_raw_files(raw_dir: Path | None = None, retention_days: int | None = None) -> int:
    """Delete zip files older than the retention window. Returns count deleted."""
    raw_dir = raw_dir or RAW_DIR
    cutoff = str(_cutoff_timestamp(retention_days))
    removed = 0

    if not raw_dir.exists():
        return 0

    for f in raw_dir.iterdir():
        m = FILENAME_RE.match(f.name)
        if m and m.group(1) < cutoff:
            f.unlink()
            removed += 1

    log.info("Removed %d old zip files", removed)
    return removed


def prune_gal_files(raw_dir: Path | None = None, retention_days: int | None = None) -> int:
    """Delete old GAL .gal.json.gz files. Returns count deleted."""
    raw_dir = raw_dir or GAL_RAW_DIR
    cutoff = str(_cutoff_timestamp(retention_days))
    removed = 0

    if not raw_dir.exists():
        return 0

    for f in raw_dir.iterdir():
        m = GAL_FILENAME_RE.match(f.name)
        if m and m.group(1) < cutoff:
            f.unlink()
            removed += 1

    log.info("Removed %d old GAL files", removed)
    return removed


def prune(
    db_path: Path | None = None,
    raw_dir: Path | None = None,
    retention_days: int | None = None,
    vacuum: bool = False,
) -> dict:
    """Run full retention cleanup on both database and raw files."""
    db_path = db_path or DB_PATH
    con = duckdb.connect(str(db_path))

    deleted = prune_database(con, retention_days)
    file_count = prune_raw_files(raw_dir, retention_days)
    gal_file_count = prune_gal_files(None, retention_days)

    if vacuum:
        log.info("Running VACUUM...")
        con.execute("VACUUM")

    con.close()

    return {**deleted, "files_removed": file_count, "gal_files_removed": gal_file_count}
