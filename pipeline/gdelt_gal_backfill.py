#!/usr/bin/env python3
"""
GDELT GAL Backfill — download and load historical GAL files.

Usage:
    python gdelt_gal_backfill.py --days 60
    python gdelt_gal_backfill.py --days 7 --workers 16
    python gdelt_gal_backfill.py --download-only
    python gdelt_gal_backfill.py --load-only
"""

import argparse
import logging
import sys

import sys
from pathlib import Path

# Run as a script from anywhere: put the repo root on sys.path so the
# `pipeline` package resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.backfill_range import resolve_range
from pipeline.config import GAL_RAW_DIR
from pipeline.gal_downloader import backfill_gal
from pipeline.gal_loader import ensure_gal_indexes, load_gal_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("gdelt_gal_backfill")


def main():
    parser = argparse.ArgumentParser(description="GDELT GAL historical backfill")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=60, help="Days to backfill (default 60)")
    group.add_argument("--since", type=str, help="Start date (YYYY-MM-DD, UTC)")
    parser.add_argument("--until", type=str, help="End date (YYYY-MM-DD, UTC), default: now (UTC)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel download threads")
    parser.add_argument("--download-only", action="store_true", help="Skip DB load")
    parser.add_argument("--load-only", action="store_true", help="Skip download")
    args = parser.parse_args()

    # Resolve date range (UTC: GDELT file timestamps are UTC)
    since, until = resolve_range(days=args.days, since=args.since, until=args.until)

    log.info("GAL backfill range (UTC): %s to %s",
             since.strftime("%Y-%m-%d %H:%M"), until.strftime("%Y-%m-%d %H:%M"))

    if not args.load_only:
        backfill_gal(
            raw_dir=GAL_RAW_DIR,
            since=since,
            until=until,
            workers=args.workers,
        )

    if not args.download_only:
        all_files = sorted(GAL_RAW_DIR.glob("*.gal.json.gz")) if GAL_RAW_DIR.exists() else []
        if not all_files:
            log.warning("No GAL files found in %s", GAL_RAW_DIR)
            return
        log.info("Loading %d GAL files into DuckDB...", len(all_files))
        summary = load_gal_batch(all_files)
        log.info(
            "Load complete — %d articles loaded, %d files processed, %d skipped, %d errors",
            summary["gal"], summary["files"], summary["skipped"], summary["errors"],
        )
        log.info("Creating GAL indexes...")
        ensure_gal_indexes()
        log.info("Indexes created.")

    log.info("GAL backfill done.")


if __name__ == "__main__":
    main()
