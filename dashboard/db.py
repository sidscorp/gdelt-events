"""DuckDB read-only connection management for the dashboard.

Each request opens its own bounded read-only connection (DuckDB allows many
parallel readers) and arms a statement-timeout watchdog so a runaway query
can't hang a worker or balloon the process. Kept separate from app.py so the
service modules (briefing, articles) can share it without importing the app.
"""

import threading
import time
from datetime import datetime, timedelta

import duckdb

from _paths import DB_PATH


def _hours_cutoff(hours):
    """Convert an hours-back window into a YYYYMMDDHHMMSS integer cutoff
    (the GDELT date format), or None for an unbounded window. Shared by the
    article feed queries (app.py) and the briefing event fetch (briefing.py)."""
    if not hours:
        return None
    return int((datetime.utcnow() - timedelta(hours=hours)).strftime("%Y%m%d%H%M%S"))

# Per-connection bounds. Each per-request DuckDB connection gets a tight
# memory limit so a runaway query can't balloon the waitress process to many
# GB; without this, per-request connections accumulated page cache across
# requests and the process drifted OOM-ward.
CONN_MEMORY_LIMIT = "1500MB"
CONN_THREADS = 4
STATEMENT_TIMEOUT_S = 35  # dashboard aborts its own slow queries at 35s


def get_db(max_retries=3):
    """Get a read-only DuckDB connection, retrying briefly on lock conflicts.

    Each connection has a bounded memory_limit + thread count to prevent the
    dashboard process from accumulating DuckDB page cache across requests.
    Read-only mode still allows multiple readers in parallel.
    """
    for attempt in range(max_retries):
        try:
            con = duckdb.connect(str(DB_PATH), read_only=True)
            try:
                con.execute(f"SET memory_limit='{CONN_MEMORY_LIMIT}'")
                con.execute(f"SET threads={CONN_THREADS}")
                # Read-only connections have no valid default spill dir on
                # Windows; point at a real one so joins that spill (e.g. the
                # event-dedup anti-join) don't fail with an invalid temp path.
                con.execute(f"SET temp_directory='{(DB_PATH.parent / 'duckdb_tmp').as_posix()}'")
            except Exception:
                pass  # older DuckDB versions may differ; not fatal
            return con
        except duckdb.IOException:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
    return None


def _arm_statement_timeout(con):
    """Install a threading.Timer that calls con.interrupt() after
    STATEMENT_TIMEOUT_S. Returns a cancel function."""
    def _kill():
        try:
            con.interrupt()
        except Exception:
            pass
    t = threading.Timer(STATEMENT_TIMEOUT_S, _kill)
    t.daemon = True
    t.start()
    return t.cancel
