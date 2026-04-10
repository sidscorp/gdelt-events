#!/usr/bin/env python3
"""
GDELT v2 Ingest — main entry point for the systemd timer.

Downloads the latest 15-minute batch, loads into DuckDB, and optionally prunes.

Usage:
    python gdelt_ingest.py              # normal incremental run
    python gdelt_ingest.py --status     # print pipeline status
    python gdelt_ingest.py --prune      # force prune now
    python gdelt_ingest.py --prune-days 30  # override retention
"""

import argparse
import fcntl
import logging
import os
import shutil
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pipeline.config import DATA_DIR, DB_PATH, LOCK_FILE, LOG_DIR, MIN_FREE_GB, RAW_DIR
from pipeline.downloader import fetch_latest, download_batch
from pipeline.loader import load_batch
from pipeline.pruner import prune


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "ingest.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])


def check_disk_space() -> float:
    """Return free disk space in GB."""
    usage = shutil.disk_usage(str(DATA_DIR))
    return usage.free / (1024 ** 3)


def acquire_lock():
    """Acquire an exclusive file lock. Returns the lock file handle or None."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except BlockingIOError:
        lock_fd.close()
        return None


def print_status() -> None:
    """Print pipeline status summary."""
    import duckdb
    from pipeline.schema import create_tables

    if not DB_PATH.exists():
        print("Database not initialized. Run an ingest or backfill first.")
        return

    con = duckdb.connect(str(DB_PATH), read_only=True)

    print(f"\nGDELT Pipeline Status")
    print(f"  Database: {DB_PATH} ({DB_PATH.stat().st_size / (1024**3):.2f} GB)")

    raw_files = list(RAW_DIR.glob("*.zip")) if RAW_DIR.exists() else []
    raw_size = sum(f.stat().st_size for f in raw_files)
    print(f"  Raw zips: {len(raw_files)} files ({raw_size / (1024**3):.2f} GB)")

    for table in ["events", "mentions", "gkg"]:
        try:
            count = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            min_ts = con.execute(f"SELECT min(CASE WHEN '{table}' = 'events' THEN \"DATEADDED\" WHEN '{table}' = 'mentions' THEN \"MentionTimeDate\" ELSE \"V1DATE\" END) FROM {table}").fetchone()[0]
            max_ts = con.execute(f"SELECT max(CASE WHEN '{table}' = 'events' THEN \"DATEADDED\" WHEN '{table}' = 'mentions' THEN \"MentionTimeDate\" ELSE \"V1DATE\" END) FROM {table}").fetchone()[0]
            print(f"  {table:>8}: {count:>12,} rows  ({min_ts} to {max_ts})")
        except Exception:
            print(f"  {table:>8}: (table not found)")

    try:
        latest = con.execute(
            "SELECT max(loaded_at) FROM _ingest_log"
        ).fetchone()[0]
        print(f"\n  Last ingest: {latest}")
    except Exception:
        pass

    print(f"  Disk free: {check_disk_space():.1f} GB")
    con.close()


def run_ingest(force_prune: bool = False, prune_days: int | None = None) -> None:
    log = logging.getLogger("gdelt_ingest")

    # Acquire lock
    lock_fd = acquire_lock()
    if not lock_fd:
        log.info("Another ingest is running, exiting.")
        return

    try:
        # Check disk space
        free_gb = check_disk_space()
        if free_gb < MIN_FREE_GB:
            log.warning("Low disk space (%.1f GB free). Running prune only.", free_gb)
            prune(retention_days=prune_days, vacuum=True)
            return

        # Download latest batch
        log.info("Fetching latest GDELT files...")
        entries = fetch_latest()
        if not entries:
            log.warning("No files returned from lastupdate.txt")
            return

        new_files = download_batch(entries)
        log.info("Downloaded %d new files", len(new_files))

        # Load any un-loaded zips into DuckDB (covers both new downloads and retries)
        all_zips = list(RAW_DIR.glob("*.zip")) if RAW_DIR.exists() else []
        if all_zips:
            summary = load_batch(all_zips)
            loaded = summary["events"] + summary["mentions"] + summary["gkg"]
            if loaded > 0 or summary["errors"] > 0:
                log.info(
                    "Loaded — events: %d, mentions: %d, gkg: %d, errors: %d",
                    summary["events"], summary["mentions"], summary["gkg"], summary["errors"],
                )

        # Prune: daily or forced
        now = datetime.now(tz=None)
        should_prune = force_prune or now.hour == 0 and now.minute < 15
        if should_prune:
            is_sunday = now.weekday() == 6
            result = prune(retention_days=prune_days, vacuum=is_sunday)
            log.info("Prune complete: %s", result)

    except Exception:
        log.exception("Ingest failed")
        sys.exit(1)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def main():
    parser = argparse.ArgumentParser(description="GDELT v2 data ingest")
    parser.add_argument("--status", action="store_true", help="Print pipeline status")
    parser.add_argument("--prune", action="store_true", help="Force prune now")
    parser.add_argument("--prune-days", type=int, help="Override retention days")
    args = parser.parse_args()

    setup_logging()

    if args.status:
        print_status()
    else:
        run_ingest(force_prune=args.prune, prune_days=args.prune_days)


if __name__ == "__main__":
    main()
