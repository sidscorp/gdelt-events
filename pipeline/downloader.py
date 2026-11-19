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


# 8 slots = the trailing 2 hours, re-checked every cycle.
RECENT_SLOTS = 8
FILE_TEMPLATES = (
    ("{ts}.export.CSV.zip", "export"),
    ("{ts}.mentions.CSV.zip", "mentions"),
    ("{ts}.gkg.csv.zip", "gkg"),
)


def slot_timestamps(newest: str, slots: int = RECENT_SLOTS) -> list[str]:
    """The `slots` 15-minute timestamps ending at `newest`, oldest first."""
    base = datetime.strptime(newest, "%Y%m%d%H%M%S")
    return [
        (base - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S")
        for i in range(slots - 1, -1, -1)
    ]


def fetch_recent_slots(slots: int = RECENT_SLOTS, raw_dir: Path | None = None) -> list[dict]:
    """Return entries for a trailing window of 15-minute slots, all file types.

    Why not just the newest slot (what fetch_latest returns): GDELT publishes
    gkg.csv.zip roughly 45+ minutes *after* the matching export file. A
    newest-slot-only fetch therefore 404s on gkg every single cycle and GKG
    silently stops being ingested forever (observed 2026-11-16 -> 11-19).
    Re-checking a short trailing window also self-heals brief outages.

    Sizes come from a HEAD request so download_file()'s size check still
    applies. Files already on disk are reported but not re-fetched (their real
    size is used), and files already loaded are skipped by load_file()'s
    _ingest_log check, so the whole path is idempotent.
    """
    raw_dir = raw_dir or RAW_DIR
    resp = requests.get(LAST_UPDATE_URL, timeout=DOWNLOAD_TIMEOUT)
    resp.raise_for_status()

    newest = None
    prefix = None
    for line in resp.text.strip().splitlines():
        entry = parse_file_entry(line)
        if not entry:
            continue
        if newest is None or entry["timestamp"] > newest:
            newest = entry["timestamp"]
            prefix = entry["url"].rsplit("/", 1)[0]
    if not newest or not prefix:
        log.warning("lastupdate.txt had no parseable entries")
        return []

    entries: list[dict] = []
    for ts in slot_timestamps(newest, slots):
        for template, file_type in FILE_TEMPLATES:
            filename = template.format(ts=ts)
            dest = raw_dir / filename
            url = f"{prefix}/{filename}"
            if dest.exists() and dest.stat().st_size > 0:
                # Include on-disk files: download_batch skips them, and the
                # caller passes them to load_batch so a file downloaded just
                # before a crash still gets loaded (load_file checks _ingest_log).
                entries.append(
                    {
                        "size": dest.stat().st_size,
                        "md5": "",
                        "url": url,
                        "filename": filename,
                        "timestamp": ts,
                        "file_type": file_type,
                        "on_disk": True,
                    }
                )
                continue
            try:
                head = requests.head(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
            except requests.RequestException:
                continue
            if head.status_code != 200:
                continue  # not published yet (normal for gkg in the newest slots)
            try:
                size = int(head.headers.get("content-length", 0))
            except (TypeError, ValueError):
                size = 0
            if size <= 0:
                continue
            entries.append(
                {
                    "size": size,
                    "md5": "",
                    "url": url,
                    "filename": filename,
                    "timestamp": ts,
                    "file_type": file_type,
                }
            )
    to_fetch = sum(1 for e in entries if not e.get("on_disk"))
    log.info(
        "Recent-slot scan: %d slots -> %d files (%d to fetch, %d already on disk)",
        slots, len(entries), to_fetch, len(entries) - to_fetch,
    )
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
