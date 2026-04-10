"""Download GDELT v2 files from the master file list."""

import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .config import (
    BACKOFF_BASE,
    DOWNLOAD_TIMEOUT,
    LAST_UPDATE_URL,
    MASTER_FILE_URL,
    MAX_RETRIES,
    RAW_DIR,
)

log = logging.getLogger(__name__)

FILENAME_RE = re.compile(r"(\d{14})\.(export|mentions|gkg)\.(CSV|csv)\.zip")


def parse_file_entry(line: str) -> dict | None:
    """Parse a master file list line into {size, md5, url, filename, timestamp, file_type}."""
    parts = line.strip().split()
    if len(parts) != 3:
        return None
    size, md5, url = parts
    filename = url.rsplit("/", 1)[-1]
    m = FILENAME_RE.match(filename)
    if not m:
        return None
    return {
        "size": int(size),
        "md5": md5,
        "url": url,
        "filename": filename,
        "timestamp": m.group(1),
        "file_type": m.group(2).lower(),
    }


def fetch_latest() -> list[dict]:
    """Fetch the 3 most recent files from lastupdate.txt (incremental runs)."""
    resp = requests.get(LAST_UPDATE_URL, timeout=DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    entries = []
    for line in resp.text.strip().splitlines():
        entry = parse_file_entry(line)
        if entry:
            entries.append(entry)
    return entries


def fetch_file_list(since: datetime, until: datetime | None = None) -> list[dict]:
    """Fetch the full master file list and filter by date range (for backfill)."""
    log.info("Fetching master file list (this may take a moment)...")
    resp = requests.get(MASTER_FILE_URL, timeout=120)
    resp.raise_for_status()

    since_str = since.strftime("%Y%m%d%H%M%S")
    until_str = (until or datetime.now(tz=None)).strftime("%Y%m%d%H%M%S")

    entries = []
    for line in resp.text.splitlines():
        entry = parse_file_entry(line)
        if entry and since_str <= entry["timestamp"] <= until_str:
            entries.append(entry)

    log.info("Found %d files in date range", len(entries))
    return entries


def download_file(url: str, dest: Path, expected_size: int) -> bool:
    """Download a single file with retries. Returns True on success."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            if dest.stat().st_size != expected_size:
                log.warning(
                    "Size mismatch for %s: got %d, expected %d",
                    dest.name, dest.stat().st_size, expected_size,
                )
                dest.unlink(missing_ok=True)
                continue
            return True
        except (requests.RequestException, OSError) as e:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, dest.name, e)
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE ** attempt)
    return False


def download_batch(file_list: list[dict], raw_dir: Path | None = None) -> list[Path]:
    """Download a batch of files, skipping those already present. Returns paths of new files."""
    raw_dir = raw_dir or RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for entry in file_list:
        dest = raw_dir / entry["filename"]
        if dest.exists() and dest.stat().st_size == entry["size"]:
            continue
        if download_file(entry["url"], dest, entry["size"]):
            downloaded.append(dest)
            log.debug("Downloaded %s", entry["filename"])
        else:
            log.error("Failed to download %s after %d retries", entry["filename"], MAX_RETRIES)

    return downloaded
