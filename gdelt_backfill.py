#!/usr/bin/env python3
"""
GDELT v2 Backfill — download and load historical data.

Usage:
    python gdelt_backfill.py --days 60
    python gdelt_backfill.py --since 2026-02-09
    python gdelt_backfill.py --since 2026-02-09 --until 2026-04-09
    python gdelt_backfill.py --days 60 --download-only   # skip DB load
    python gdelt_backfill.py --days 60 --load-only       # load existing zips
    python gdelt_backfill.py --days 60 --workers 8       # more parallel downloads
"""

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.config import BACKFILL_WORKERS, DB_PATH, RAW_DIR
from pipeline.downloader import download_file, fetch_file_list
from pipeline.loader import load_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("gdelt_backfill")


def download_parallel(entries: list[dict], raw_dir: Path, workers: int) -> list[Path]:
    """Download files in parallel, skipping existing. Returns paths of new files."""
    raw_dir.mkdir(parents=True, exist_ok=True)

    to_download = []
    for entry in entries:
        dest = raw_dir / entry["filename"]
        if dest.exists() and dest.stat().st_size == entry["size"]:
            continue
        to_download.append(entry)

    if not to_download:
        log.info("All %d files already present, nothing to download.", len(entries))
        return []

    total_mb = sum(e["size"] for e in to_download) / (1024 * 1024)
    log.info(
        "Downloading %d files (%.1f MB) with %d workers — skipping %d existing",
        len(to_download), total_mb, workers, len(entries) - len(to_download),
    )

    downloaded = []
    failed = 0

    def _download(entry):
        dest = raw_dir / entry["filename"]
        return entry["filename"], download_file(entry["url"], dest, entry["size"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download, e): e for e in to_download}
        done = 0
        for future in as_completed(futures):
            done += 1
            filename, success = future.result()
            if success:
                downloaded.append(raw_dir / filename)
            else:
                failed += 1
            if done % 100 == 0 or done == len(to_download):
                log.info("Progress: %d/%d (%.0f%%), %d failed", done, len(to_download), done / len(to_download) * 100, failed)

    log.info("Download complete: %d succeeded, %d failed", len(downloaded), failed)
    return downloaded


def main():
    parser = argparse.ArgumentParser(description="GDELT v2 historical backfill")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--days", type=int, help="Number of days to look back")
    group.add_argument("--since", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", type=str, help="End date (YYYY-MM-DD), default: now")
    parser.add_argument("--workers", type=int, default=BACKFILL_WORKERS, help="Parallel download threads")
    parser.add_argument("--download-only", action="store_true", help="Download only, skip DB load")
    parser.add_argument("--load-only", action="store_true", help="Load existing zips, skip download")
    args = parser.parse_args()

    # Resolve date range
    if args.days:
        since = datetime.now(tz=None) - timedelta(days=args.days)
    else:
        since = datetime.strptime(args.since, "%Y-%m-%d")

    until = datetime.strptime(args.until, "%Y-%m-%d") if args.until else datetime.now(tz=None)

    log.info("Backfill range: %s to %s", since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d"))

    if not args.load_only:
        # Fetch file list and download
        entries = fetch_file_list(since, until)
        if not entries:
            log.error("No files found in date range")
            sys.exit(1)

        total_mb = sum(e["size"] for e in entries) / (1024 * 1024)
        log.info("Found %d files (%.1f MB total)", len(entries), total_mb)

        new_files = download_parallel(entries, RAW_DIR, args.workers)
    else:
        new_files = []

    if not args.download_only:
        # Load all zips in raw dir into DuckDB
        all_zips = sorted(RAW_DIR.glob("*.zip")) if RAW_DIR.exists() else []
        if not all_zips:
            log.warning("No zip files found in %s", RAW_DIR)
            return

        log.info("Loading %d zip files into DuckDB...", len(all_zips))
        summary = load_batch(all_zips)
        log.info(
            "Load complete — events: %d, mentions: %d, gkg: %d, errors: %d",
            summary["events"], summary["mentions"], summary["gkg"], summary["errors"],
        )

    log.info("Backfill done.")


if __name__ == "__main__":
    main()
