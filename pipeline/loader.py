"""Load GDELT zip files into DuckDB.

Lock behaviour (documented — this is the ingest's queueing contract)
-------------------------------------------------------------------
DuckDB allows one writer OR many readers, and a *reader* that stays open blocks the
writer. Bulk loads therefore queue behind the database file's write lock instead of
failing on the first collision, for a budget the caller sets (see
`BULK_LOCK_WAIT_BUDGET_S` and `LOCK_WAIT_BUDGET_S`). When the wait runs out the loader
raises `DbLockTimeout` carrying the competing process's PID, executable and command
line — or "holder unknown" — so the caller can name it and the next occurrence needs no
guesswork. The ingest (gdelt_ingest.py) sets its own, much smaller budget from the
scheduled task's 10-minute execution limit and turns a timeout into a deferral.
"""

import logging
import os
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import duckdb

from .config import DB_PATH
from .downloader import FILENAME_RE
from .schema import TABLE_MAP, create_tables, read_csv_sql, select_columns

log = logging.getLogger(__name__)

# Commit every N files to bound memory and let WAL flush.
# Smaller = lower memory, more disk churn. Larger = faster but more RAM.
BATCH_COMMIT_SIZE = 50

# Cap DuckDB memory. The gpu-host has 48 GB total; 8 GB leaves plenty for
# Frigate + Ollama + other workloads.
DUCKDB_MEMORY_LIMIT = "8GB"

# How long an open may wait for the single-writer lock.
#
# Three numbers, because these processes have different jobs and different deadlines:
#   * LOCK_WAIT_BUDGET_S (90 s) is the old ~82 s patience, kept for the short-lived
#     writers inside a cycle (tagger, FDA matcher, the pill write burst). Waiting half
#     an hour inside work that is already running would only stretch the cycle.
#   * BULK_LOCK_WAIT_BUDGET_S (1800 s) is for backfills, which are run by hand from an
#     ssh session and have no task deadline. It is sized from measurement: the ingest's
#     competitor is `pill_scorer`, which holds a read-only connection across its LLM
#     judging loop (data/logs/embed_new.log `elapsed_s`, three days to 2026-11-22, 195
#     runs: median 302 s, p95 1041 s, max 1760 s).
#   * The INGEST's own budget is computed per cycle in gdelt_ingest.py and is MUCH
#     smaller, because GDELT-Ingest is registered with ExecutionTimeLimit PT10M:
#     Task Scheduler kills the process at 10 minutes, so a wait longer than ~5 minutes
#     would be killed mid-wait and lose the cycle silently — worse than failing.
BULK_LOCK_WAIT_BUDGET_S = 1800.0
LOCK_WAIT_BUDGET_S = 90.0
LOCK_RETRY_MAX_SLEEP_S = 5.0
# Progress line this often while waiting, so a long queue is visible in the log.
LOCK_WAIT_PROGRESS_EVERY_S = 60.0

# A failed open can mean "another process holds it" or a real IO fault. Only the
# former deserves the bulk budget; a full disk must not stall a load for 30 minutes.
_LOCK_HINTS = (
    "being used by another process",
    "File is already open in",
    "Could not set lock",
    "Conflicting lock",
)
_HOLDER_RE = re.compile(
    r"File is already open in\s+(?P<exe>.+?)\s*\(PID (?P<pid>\d+)\)", re.S
)
_PID_RE = re.compile(r"\(PID (\d+)\)")


class DbLockTimeout(RuntimeError):
    """The database file stayed locked for the whole wait budget.

    Carries the identity of the competing process (or "holder unknown") so the
    top-level handler can log it next to the error.
    """

    def __init__(self, message: str, *, holder: str, waited_s: float, attempts: int,
                 last_error: BaseException | None = None):
        super().__init__(message)
        self.holder = holder
        self.waited_s = waited_s
        self.attempts = attempts
        self.last_error = last_error


def is_lock_conflict(exc: BaseException) -> bool:
    """True when the error says another process holds the database file."""
    text = str(exc)
    return any(hint in text for hint in _LOCK_HINTS)


def holder_from_error(exc: BaseException) -> tuple[str | None, int | None]:
    """Extract (executable, pid) that DuckDB named as the file holder.

    DuckDB either names the holder ("File is already open in <exe> (PID n)") or only
    repeats the Windows message; both shapes are handled.
    """
    text = str(exc)
    m = _HOLDER_RE.search(text)
    if m:
        return m.group("exe").strip(), int(m.group("pid"))
    m = _PID_RE.search(text)
    if m:
        return None, int(m.group(1))
    return None, None


def _pid_image(pid: int) -> str | None:
    """Executable path of a process, without spawning anything."""
    if os.name != "nt":
        return None
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = ctypes.c_uint(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value or None
        return None
    finally:
        kernel32.CloseHandle(handle)


# One command-line lookup per PID per process: resolving it costs a PowerShell start.
_CMDLINE_CACHE: dict[int, str | None] = {}


def _pid_command_line(pid: int) -> str | None:
    """Command line of a process, or None when it cannot be read."""
    pid = int(pid)
    if pid in _CMDLINE_CACHE:
        return _CMDLINE_CACHE[pid]

    cmd = None
    if os.name != "nt":
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
        except OSError:
            cmd = None
    else:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            cmd = (out.stdout or "").strip() or None
        except (OSError, subprocess.SubprocessError):
            cmd = None
    _CMDLINE_CACHE[pid] = cmd
    return cmd


def describe_holder(exe: str | None, pid: int | None) -> str:
    """Human-readable identity of the competing process, never empty."""
    if pid is None:
        return "holder unknown (DuckDB named no process)"
    exe = exe or _pid_image(pid)
    cmd = _pid_command_line(pid)
    if cmd:
        if len(cmd) > 300:
            cmd = cmd[:297] + "..."
        return f"{exe or 'unknown exe'} (PID {pid}) command line: {cmd}"
    if exe:
        return f"{exe} (PID {pid}) command line unavailable"
    return f"PID {pid}: image and command line unavailable"


def _short_holder(exe: str | None, pid: int | None) -> str:
    if pid is None:
        return "holder unknown"
    return f"{exe or 'unknown exe'} (PID {pid})"


def _configure(con: duckdb.DuckDBPyConnection, db_path: Path | None = None) -> None:
    """Apply memory limits and other pragmas."""
    con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
    con.execute("SET threads = 2")  # Keep CPU usage modest on the shared box
    # Give CTAS / GROUP BY / QUALIFY a writable scratch dir (Windows default
    # is blank and fails on operations that need to spill). It lives next to the
    # database file, which is DATA_DIR in production and a temp dir under test.
    base = (db_path.parent if db_path is not None else DB_PATH.parent) / "duckdb_tmp"
    base.mkdir(parents=True, exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{base.as_posix()}'")


def _open_connection(db_path: Path, budget_s: float | None = None) -> duckdb.DuckDBPyConnection:
    """Open DuckDB for write, waiting out a reader that holds the file.

    DuckDB is single-writer. During a bulk load the dashboard, the embedder and the
    pill scorer all hold read-only connections; a writer's open fails for as long as
    any of them lives. This queues behind that lock for `budget_s` seconds (default:
    the old ~82 s patience) and names the holder in the log while it waits.

    Raises DbLockTimeout when the budget is spent, carrying the holder's identity.
    """
    budget = LOCK_WAIT_BUDGET_S if budget_s is None else float(budget_s)
    started = time.monotonic()
    deadline = started + budget
    attempt = 0
    last_err: BaseException | None = None
    holder_short = "holder unknown"
    next_progress = started + LOCK_WAIT_PROGRESS_EVERY_S

    while True:
        attempt += 1
        try:
            con = duckdb.connect(str(db_path))
            _configure(con, db_path)
            create_tables(con)
            waited = time.monotonic() - started
            if attempt > 1:
                log.warning(
                    "DuckDB opened after waiting %.0fs (%d attempts); released by %s",
                    waited, attempt, holder_short,
                )
            return con
        except duckdb.IOException as e:
            last_err = e
            exe, pid = holder_from_error(e)
            holder_short = _short_holder(exe, pid)
            if not is_lock_conflict(e):
                # A real IO fault, not contention: fail at the old pace rather than
                # burning the bulk budget on it.
                budget = min(budget, LOCK_WAIT_BUDGET_S)
                deadline = min(deadline, started + budget)
            now = time.monotonic()
            if attempt == 1:
                log.warning(
                    "DB lock conflict opening %s: %s; waiting up to %.0fs",
                    db_path, holder_short, budget,
                )
            elif now >= next_progress:
                log.warning(
                    "Still waiting for %s to release %s (%.0fs of %.0fs)",
                    holder_short, db_path, now - started, budget,
                )
                next_progress = now + LOCK_WAIT_PROGRESS_EVERY_S

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(min(LOCK_RETRY_MAX_SLEEP_S, 1.0 + attempt * 0.5), remaining))

    waited = time.monotonic() - started
    exe, pid = holder_from_error(last_err) if last_err is not None else (None, None)
    holder = describe_holder(exe, pid)
    message = (
        f"could not open {db_path} for write after {waited:.0f}s "
        f"({attempt} attempts); competing holder: {holder}"
    )
    # WARNING, not ERROR: this is a bounded timeout whose severity depends on the
    # caller. The ingest turns it into an explicit, named deferral (exit 3); a
    # backfill that treats it as fatal logs its own error and this message rides
    # along in the traceback.
    log.warning("%s; last error: %s", message, last_err)
    raise DbLockTimeout(
        message, holder=holder, waited_s=waited, attempts=attempt, last_error=last_err,
    ) from last_err


def classify_file(path: Path) -> tuple[str, str] | None:
    """Extract (file_type, timestamp) from a zip filename. Returns None if unrecognized."""
    m = FILENAME_RE.match(path.name)
    if not m:
        return None
    return m.group(2).lower(), m.group(1)


def is_loaded(con: duckdb.DuckDBPyConnection, filename: str) -> bool:
    """Check if a file has already been ingested."""
    result = con.execute(
        "SELECT 1 FROM _ingest_log WHERE filename = ?", [filename]
    ).fetchone()
    return result is not None


def load_file(con: duckdb.DuckDBPyConnection, zip_path: Path, tmp_dir: str) -> int:
    """Load a single zip file into the appropriate DuckDB table. Returns row count."""
    info = classify_file(zip_path)
    if not info:
        log.warning("Skipping unrecognized file: %s", zip_path.name)
        return 0

    file_type, timestamp = info
    table_name = TABLE_MAP[file_type][0]

    if is_loaded(con, zip_path.name):
        return 0

    # Extract CSV from zip archive (DuckDB doesn't support zip, only gzip)
    with zipfile.ZipFile(zip_path) as zf:
        inner_name = zf.namelist()[0]
        zf.extract(inner_name, tmp_dir)
        csv_path = Path(tmp_dir) / inner_name

    try:
        csv_sql = read_csv_sql(str(csv_path), file_type)
        cols = select_columns(file_type)

        # Single INSERT with subquery counts + inserts in one pass.
        result = con.execute(
            f"INSERT INTO {table_name} SELECT {cols} FROM {csv_sql} RETURNING 1"
        ).fetchall()
        row_count = len(result)

        con.execute(
            "INSERT INTO _ingest_log (filename, file_type, batch_timestamp, row_count) "
            "VALUES (?, ?, ?, ?)",
            [zip_path.name, file_type, int(timestamp), row_count],
        )

        return row_count
    finally:
        csv_path.unlink(missing_ok=True)


def load_batch(zip_files: list[Path], db_path: Path | None = None,
               lock_budget_s: float | None = None) -> dict:
    """Load multiple zip files into DuckDB with periodic commits to bound memory.

    Opens a fresh connection every BATCH_COMMIT_SIZE files, which forces
    DuckDB to flush its WAL and release buffers. This keeps RAM under control
    when loading thousands of files in one backfill run.

    Waits up to `lock_budget_s` (default BULK_LOCK_WAIT_BUDGET_S) for readers to let
    go of the single-writer lock; see the module docstring.
    """
    db_path = db_path or DB_PATH
    budget = BULK_LOCK_WAIT_BUDGET_S if lock_budget_s is None else float(lock_budget_s)
    summary = {"events": 0, "mentions": 0, "gkg": 0, "skipped": 0, "errors": 0}
    total = len(zip_files)

    # Sort so we process chronologically and in batches of adjacent files
    sorted_files = sorted(zip_files)

    with tempfile.TemporaryDirectory(prefix="gdelt_") as tmp_dir:
        con = _open_connection(db_path, budget_s=budget)
        processed = 0

        try:
            for i, zip_path in enumerate(sorted_files, start=1):
                try:
                    info = classify_file(zip_path)
                    if not info:
                        summary["skipped"] += 1
                        continue
                    file_type = info[0]
                    table_name = TABLE_MAP[file_type][0]
                    rows = load_file(con, zip_path, tmp_dir)
                    summary[table_name] += rows
                    processed += 1
                except Exception:
                    log.exception("Error loading %s", zip_path.name)
                    summary["errors"] += 1

                # Periodic checkpoint: close and reopen to flush memory
                if i % BATCH_COMMIT_SIZE == 0:
                    con.execute("CHECKPOINT")
                    con.close()
                    log.info(
                        "Progress: %d/%d loaded (events=%d, mentions=%d, gkg=%d, errors=%d)",
                        i, total, summary["events"], summary["mentions"],
                        summary["gkg"], summary["errors"],
                    )
                    con = _open_connection(db_path, budget_s=budget)
        finally:
            try:
                con.execute("CHECKPOINT")
                con.close()
            except Exception:
                pass

    return summary
