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
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import sys
from pathlib import Path

# Run as a script from anywhere: put the repo root on sys.path so the
# `pipeline` package resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.config import DATA_DIR, DB_PATH, LOCK_FILE, LOG_DIR, MIN_FREE_GB, RAW_DIR
from pipeline.downloader import fetch_latest, fetch_recent_slots, download_batch
from pipeline.loader import DbLockTimeout, load_batch
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


# The host registers GDELT-Ingest with ExecutionTimeLimit PT10M (see
# deploy/tasks/GDELT-Ingest.xml). Task Scheduler kills the process at that limit, so a
# lock wait that overruns it loses the cycle silently AND without an "Ingest failed"
# line - the exact failure shape issue #42 exists to prevent. The wait is therefore
# clamped to the window the task actually has left, and the cycle aims to finish inside
# 90% of the limit. Measured 2026-11-22 since 11-20 (n=256 cycles): work takes median
# 79 s, p99 152 s, max 386 s, so the 240 s reserve covers the work after the wait.
# With the 240 s ceiling and a typical 20-60 s of download and tagging around it, the
# cycle stays well inside 300 s of wait + work, leaving >=300 s of the hard limit as
# margin for a slow load.
INGEST_TASK_TIME_LIMIT_S = 600.0
INGEST_CYCLE_TARGET_S = 540.0
INGEST_LOCK_RESERVE_S = 240.0
INGEST_LOCK_WAIT_WANT_S = 240.0
DEFER_EXIT_CODE = 3  # non-zero: a deferral is not success (see #42)


def ingest_lock_budget(cycle_started: float, now: float | None = None,
                       want: float = INGEST_LOCK_WAIT_WANT_S) -> float:
    """Seconds this cycle may spend queueing behind a reader, within its task window."""
    now = time.monotonic() if now is None else now
    headroom = INGEST_CYCLE_TARGET_S - INGEST_LOCK_RESERVE_S - (now - cycle_started)
    return max(0.0, min(want, headroom))


def check_disk_space() -> float:
    """Return free disk space in GB."""
    usage = shutil.disk_usage(str(DATA_DIR))
    return usage.free / (1024 ** 3)


def acquire_lock():
    """Cross-platform PID-file lock. Returns a lock handle (file path) or None.

    If a lock file exists with a live PID, return None.
    Otherwise, write our PID and return the path for release.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            # Check if the PID is still alive
            if _pid_alive(old_pid):
                return None
            # Stale lock — previous run crashed
        except (ValueError, OSError):
            pass  # Corrupt lock file, overwrite it

    LOCK_FILE.write_text(str(os.getpid()))
    return LOCK_FILE


def _pid_alive(pid: int) -> bool:
    """Return True only if a *running* process with this PID exists.

    OpenProcess() alone is not enough on Windows. A process that exits with a
    fail-fast code (e.g. 0xC0000409) can linger as a zombie object while any
    handle to it is still open: OpenProcess succeeds, so the old check called a
    long-dead ingest "alive" and refused to start a new one forever. That wedged
    ingestion for three days in Nov 2026. GetExitCodeProcess distinguishes a
    running process (STILL_ACTIVE) from a terminated one.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists but belongs to another account.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def release_lock(lock_handle):
    """Release the PID lock file."""
    if lock_handle and lock_handle.exists():
        try:
            lock_handle.unlink()
        except OSError:
            pass


def lock_holder() -> int | None:
    """PID recorded in the lock file, or None if it is missing or unreadable."""
    try:
        return int(LOCK_FILE.read_text().strip())
    except (OSError, ValueError):
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
        # Declining to run is not success. This used to log at INFO and return, so the
        # 69-hour stall of Nov 2026 looked like 277 healthy cycles that each exited 0.
        # Exit 2 (not 1) keeps "declined because another ingest holds the lock" distinct
        # from "ran and failed", and names the holder so the next reader does not have
        # to go looking for it.
        holder = lock_holder()
        log.error(
            "Another ingest is running: %s held by PID %s. Declining to run; exiting 2.",
            LOCK_FILE, holder if holder is not None else "unknown",
        )
        sys.exit(2)

    try:
        cycle_started = time.monotonic()

        # Check disk space
        free_gb = check_disk_space()
        if free_gb < MIN_FREE_GB:
            log.warning("Low disk space (%.1f GB free). Running prune only.", free_gb)
            prune(retention_days=prune_days, vacuum=True)
            return

        # Download the trailing window of slots (not just the newest): gkg is
        # published ~45+ min late, so a newest-slot-only fetch skips it forever.
        log.info("Fetching latest GDELT files...")
        entries = fetch_recent_slots()
        if not entries:
            # Nothing new is downloadable; still fall through so the GAL pass
            # and prune below run as usual.
            log.info("No new files in the recent-slot window")

        new_files = download_batch(entries)
        log.info("Downloaded %d new files", len(new_files))
        data_changed = False

        # Load every file in the window, not only the ones downloaded this
        # cycle: load_file() skips anything already recorded in _ingest_log, so
        # a file whose download succeeded just before a crash still gets loaded.
        window_files = [
            RAW_DIR / e["filename"] for e in entries
            if (RAW_DIR / e["filename"]).exists()
        ]
        if window_files:
            summary = load_batch(window_files,
                                 lock_budget_s=ingest_lock_budget(cycle_started))
            log.info(
                "Loaded — events: %d, mentions: %d, gkg: %d, errors: %d (window: %d files)",
                summary["events"], summary["mentions"], summary["gkg"], summary["errors"],
                len(window_files),
            )
            if summary["events"] or summary["mentions"] or summary["gkg"]:
                data_changed = True

        # GAL (Global Article List) — broader coverage, no NER. Non-fatal.
        try:
            from pipeline.gal_downloader import fetch_recent_gal
            from pipeline.gal_loader import load_gal_batch

            gal_files = fetch_recent_gal(minutes_back=20)
            if gal_files:
                gal_summary = load_gal_batch(
                    gal_files, lock_budget_s=ingest_lock_budget(cycle_started),
                )
                log.info(
                    "GAL loaded: %d articles from %d files (skipped %d, errors %d)",
                    gal_summary["gal"], gal_summary["files"],
                    gal_summary["skipped"], gal_summary["errors"],
                )
                if gal_summary["gal"]:
                    data_changed = True
        except DbLockTimeout as exc:
            # GAL is what the freshness gate reads, so say so plainly - but a GAL
            # deferral in a cycle that already loaded events/GKG is not a failure.
            log.warning("GAL deferred: %s", exc)
        except Exception:
            log.exception("GAL ingest failed (non-fatal)")

        # FDA company match — incremental, only scans rows newer than last run.
        # Non-fatal: the dashboard view will just miss new matches if this fails.
        try:
            from pipeline.fda_matcher import match_new_rows
            fda_summary = match_new_rows()
            log.info("FDA match: %s", fda_summary)
        except Exception:
            log.exception("FDA matcher failed (non-fatal)")

        # Supply chain + topic tagging - incremental keyword scan on new articles.
        try:
            from pipeline.tagger import tag_new_rows as tag_articles
            tag_summary = tag_articles()
            log.info("Tagger: %s", tag_summary)
        except Exception:
            log.exception("Tagger failed (non-fatal)")

        # Bump the data-version marker AFTER new rows are loaded and tagged, so
        # the dashboard's cache invalidation (articles.py::_feed_cache_key) only
        # sees a new version once the data behind it is actually query-ready.
        if data_changed:
            try:
                (DATA_DIR / "data_version.txt").write_text(str(int(time.time())))
            except Exception:
                log.exception("Failed to write data_version.txt (non-fatal)")

        # FDA regulatory events — polls OpenFDA API every 6 hours.
        # Non-fatal: dashboard view shows cached events if this fails.
        now_for_fda = datetime.now()
        if now_for_fda.hour % 6 == 0:
            try:
                from pipeline.fda_events import ingest_fda_events
                fda_ev_summary = ingest_fda_events()
                log.info("FDA events: %s", fda_ev_summary)
            except Exception:
                log.exception("FDA events ingest failed (non-fatal)")

                # Prune: daily or forced
        now = datetime.now(tz=None)
        should_prune = force_prune or now.hour == 0 and now.minute < 15
        if should_prune:
            is_sunday = now.weekday() == 6
            result = prune(retention_days=prune_days, vacuum=is_sunday)
            log.info("Prune complete: %s", result)

    except DbLockTimeout as exc:
        # A held database is not a failure: it is a named deferral. The next cycle
        # (15 minutes away, MultipleInstancesPolicy=IgnoreNew) re-reads the trailing
        # two-hour window from 5920e81, so the slot the deferral skipped is ingested
        # then - no data is lost. Exit 3 keeps it distinct from success (issue #42:
        # declining must never look like success), and the holder's PID/command line
        # goes in the line so the next occurrence needs no archaeology. This is why
        # the budget is bounded: the task is killed at ExecutionTimeLimit PT10M.
        log.warning(
            "Ingest deferred (database held): %s. Next cycle re-reads the 2h window; "
            "exit %d.", exc, DEFER_EXIT_CODE,
        )
        sys.exit(DEFER_EXIT_CODE)
    except Exception:
        log.exception("Ingest failed")
        sys.exit(1)
    finally:
        release_lock(lock_fd)


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
