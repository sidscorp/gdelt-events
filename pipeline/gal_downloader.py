"""Download GDELT v3 GAL (Global Article List) files.

GAL provides broader article coverage than GKG but without entity extraction.
Files are published ~3 per 15-min boundary at minutes :01/:02/:03 after each
quarter-hour (verified 2020-01-01 and 2026-04-10). That's ~12 files/hour.

Filename format: YYYYMMDDHHMMSS.gal.json.gz
URL: http://data.gdeltproject.org/gdeltv3/gal/{timestamp}.gal.json.gz
Each file: ~200 KB gzipped JSON-NL, ~800 records.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .config import (
    BACKOFF_BASE,
    DOWNLOAD_TIMEOUT,
    GAL_FEED_URL,
    GAL_FILE_URL_TEMPLATE,
    GAL_RAW_DIR,
    MAX_RETRIES,
)

log = logging.getLogger(__name__)

# Based on probing GDELT's archive: files cluster at HH:MM:00 where MM is one
# of these values (minutes :01/:02/:03 after each 15-min boundary).
EXPECTED_MINUTES = [1, 2, 3, 16, 17, 18, 31, 32, 33, 46, 47, 48]

GAL_FILENAME_RE = re.compile(r"(\d{14})\.gal\.json\.gz")
RSS_BUILDDATE_RE = re.compile(r"<lastBuildDate>(.*?)</lastBuildDate>")


def fetch_latest_gal_filename() -> str | None:
    """Fetch the RSS feed and extract the latest GAL file timestamp.

    The RSS <lastBuildDate> matches the latest published filename to the second.
    Returns a YYYYMMDDHHMMSS string or None on failure.
    """
    try:
        resp = requests.get(GAL_FEED_URL, timeout=DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        m = RSS_BUILDDATE_RE.search(resp.text)
        if not m:
            log.warning("No <lastBuildDate> in GAL feed")
            return None
        # Format: "10 Apr 2026 23:32:00 +0000"
        dt = datetime.strptime(m.group(1).strip(), "%d %b %Y %H:%M:%S %z")
        return dt.strftime("%Y%m%d%H%M%S")
    except (requests.RequestException, ValueError) as e:
        log.warning("fetch_latest_gal_filename failed: %s", e)
        return None


def expected_gal_timestamps(since: datetime, until: datetime) -> list[str]:
    """Yield expected GAL filename timestamps in [since, until].

    Only returns timestamps where files are likely to exist (the 12
    slots per hour empirically confirmed). Drops a naive minute-by-minute
    iteration by ~80%.
    """
    result = []
    cursor = since.replace(minute=0, second=0, microsecond=0)
    while cursor <= until:
        for minute in EXPECTED_MINUTES:
            ts = cursor.replace(minute=minute)
            if since <= ts <= until:
                result.append(ts.strftime("%Y%m%d%H%M%S"))
        cursor += timedelta(hours=1)
    return result


def download_gal_file(timestamp: str, dest_dir: Path | None = None) -> Path | None:
    """Download a single GAL file. Returns the local path or None on failure/404."""
    dest_dir = dest_dir or GAL_RAW_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{timestamp}.gal.json.gz"

    # Skip if already present (any size > 0)
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    url = GAL_FILE_URL_TEMPLATE.format(timestamp=timestamp)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
            if resp.status_code == 404:
                return None  # Missing file — expected for some minutes
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            if dest.stat().st_size < 100:
                log.warning("Suspiciously small GAL file: %s (%d bytes)", dest.name, dest.stat().st_size)
                dest.unlink(missing_ok=True)
                return None
            return dest
        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
            else:
                log.warning("Download failed for %s after %d retries: %s", timestamp, MAX_RETRIES, e)
    return None


def fetch_recent_gal(raw_dir: Path | None = None, minutes_back: int = 60) -> list[Path]:
    """Catch-up download: iterate expected timestamps in the last N minutes
    and fetch any that return 200. Returns list of downloaded paths.
    """
    raw_dir = raw_dir or GAL_RAW_DIR
    now = datetime.utcnow()
    since = now - timedelta(minutes=minutes_back)
    timestamps = expected_gal_timestamps(since, now)

    downloaded = []
    for ts in timestamps:
        p = download_gal_file(ts, raw_dir)
        if p:
            downloaded.append(p)
    return downloaded


def backfill_gal(
    raw_dir: Path | None = None,
    days: int = 60,
    workers: int = 8,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Path]:
    """Parallel download of historical GAL files."""
    raw_dir = raw_dir or GAL_RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    until = until or datetime.utcnow()
    since = since or (until - timedelta(days=days))

    timestamps = expected_gal_timestamps(since, until)
    log.info(
        "GAL backfill: %d expected timestamps from %s to %s",
        len(timestamps),
        since.strftime("%Y-%m-%d %H:%M"),
        until.strftime("%Y-%m-%d %H:%M"),
    )

    downloaded = []
    missing = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download_gal_file, ts, raw_dir): ts for ts in timestamps}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                p = future.result()
                if p:
                    downloaded.append(p)
                else:
                    missing += 1
            except Exception as e:
                log.debug("Download error: %s", e)
                missing += 1
            if done % 200 == 0 or done == len(timestamps):
                log.info(
                    "GAL progress: %d/%d (%.0f%%), %d downloaded, %d missing",
                    done, len(timestamps), done / len(timestamps) * 100,
                    len(downloaded), missing,
                )

    log.info("GAL backfill complete: %d downloaded, %d missing", len(downloaded), missing)
    return downloaded
