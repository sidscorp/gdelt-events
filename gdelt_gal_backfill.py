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
from datetime import datetime, timedelta

from pipeline.config import GAL_RAW_DIR
from pipeline.gal_downloader import backfill_gal
from pipeline.gal_loader import load_gal_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("gdelt_gal_backfill")


def main():
    parser = argparse.ArgumentParser(description="GDELT GAL historical backfill")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=60, help="Days to backfill (default 60)")
    group.add_argument("--since", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel download threads")
    parser.add_argument("--download-only", action="store_true", help="Skip DB load")
    parser.add_argument("--load-only", action="store_true", help="Skip download")
    args = parser.parse_args()

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d")
    else:
        since = datetime.utcnow() - timedelta(days=args.days)
    until = datetime.strptime(args.until, "%Y-%m-%d") if args.until else datetime.utcnow()

    log.info("GAL backfill range: %s to %s",
             since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d"))

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

    log.info("GAL backfill done.")


if __name__ == "__main__":
    main()
