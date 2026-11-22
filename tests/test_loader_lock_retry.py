"""The ingest must wait out a reader instead of losing the cycle — and name the holder.

Measured 2026-11-22 (`data/logs/ingest.log*`): 159 `Ingest failed` lines, 155 of them
`_duckdb.IOException: ... being used by another process`, at 26 of 96 cycles that day
alone. The competing process was the pill scorer (a child of `embed_new_articles.py`),
which keeps a read-only DuckDB connection open across its LLM judging loop — measured
median 302 s, p95 1041 s, max 1760 s over three days. The loader's old budget was 20
attempts (~82 s), so every hold longer than that cost a cycle.

DuckDB's Windows file lock cannot be simulated inside one process, so this test starts
a real second interpreter that holds the write connection. That also makes the
give-up test exercise the real holder message rather than a fabricated one.

Run with the repo venv (duckdb lives there):
    .venv\\Scripts\\python.exe -m pytest tests\\test_loader_lock_retry.py -v
A plain run works too (no pytest required):
    .venv\\Scripts\\python.exe tests\\test_loader_lock_retry.py
"""
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import loader  # noqa: E402

# Run in a second interpreter: open the database for write and hold it.
HOLDER_SCRIPT = """
import os, sys, time
import duckdb

db_path, ready_path, hold_s = sys.argv[1], sys.argv[2], float(sys.argv[3])
con = duckdb.connect(db_path)
con.execute("CREATE TABLE IF NOT EXISTS holder_probe (i INTEGER)")
con.execute("INSERT INTO holder_probe VALUES (1)")
con.execute("CHECKPOINT")
# The venv's python.exe re-execs the base interpreter, so the process that actually
# holds the lock is NOT the Popen handle. Publish its own PID for the assertions.
open(ready_path, "w", encoding="utf-8").write(str(os.getpid()))
time.sleep(hold_s)
con.close()
"""


class _Recorder(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


def _capture():
    """Collect pipeline.loader log records; returns (recorder, restore)."""
    rec = _Recorder()
    logger = logging.getLogger("pipeline.loader")
    logger.addHandler(rec)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)

    def restore():
        logger.removeHandler(rec)
        logger.setLevel(old_level)

    return rec, restore


def _start_holder(tmp: Path, hold_s: float):
    """Start a real second process holding the write lock.

    Returns (launcher_proc, db_path, holder_pid, script_path) where holder_pid is the
    interpreter that actually owns the lock (a child of the Popen handle on this host).
    """
    db_path = tmp / f"lock_{uuid.uuid4().hex[:8]}.duckdb"
    ready = tmp / f"ready_{uuid.uuid4().hex[:8]}"
    script = tmp / "holder.py"
    script.write_text(HOLDER_SCRIPT, encoding="utf-8")
    err = tmp / f"err_{uuid.uuid4().hex[:8]}.log"
    with open(err, "wb") as err_fh:
        proc = subprocess.Popen(
            [sys.executable, str(script), str(db_path), str(ready), str(hold_s)],
            stdout=subprocess.DEVNULL, stderr=err_fh,
        )
    deadline = time.monotonic() + 30
    while not ready.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise AssertionError(
                f"holder process never took the lock (exit={proc.returncode}): "
                f"{err.read_text(encoding='utf-8', errors='replace')}"
            )
        time.sleep(0.05)
    holder_pid = int(ready.read_text(encoding="utf-8").strip())
    return proc, db_path, holder_pid, script


def _stop(proc, holder_pid=None):
    """Kill the holder interpreter and its launcher, then wait for both to go."""
    pids = [p for p in (holder_pid, getattr(proc, "pid", None)) if p]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_waits_for_a_writer_and_then_succeeds():
    """A held write lock is waited out, not failed on."""
    rec, restore = _capture()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        proc, db_path, holder_pid, _script = _start_holder(tmp, hold_s=4.0)
        try:
            started = time.monotonic()
            con = loader._open_connection(db_path, budget_s=60)
            elapsed = time.monotonic() - started
            con.close()
        finally:
            _stop(proc, holder_pid)
            restore()

    assert elapsed >= 3.0, f"opened after only {elapsed:.1f}s — it did not wait for the holder"
    assert elapsed < 30, f"waited {elapsed:.1f}s for a 4s hold"
    text = rec.text()
    assert "DB lock conflict" in text, text
    assert "opened after waiting" in text, text
    assert f"PID {holder_pid}" in text, f"holder PID not named: {text}"
    assert "holder unknown" not in text, text


def test_gives_up_after_budget_and_names_the_holder():
    """When the budget is spent the failure names the competitor, not an empty error."""
    rec, restore = _capture()
    raised = None
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            proc, db_path, holder_pid, script = _start_holder(Path(td), hold_s=30.0)
            try:
                started = time.monotonic()
                loader._open_connection(db_path, budget_s=1.5)
            except loader.DbLockTimeout as exc:
                raised = exc
                elapsed = time.monotonic() - started
            finally:
                _stop(proc, holder_pid)
    finally:
        restore()

    assert raised is not None, "expected DbLockTimeout, got a connection or another error"
    assert elapsed < 20, f"budget 1.5s but it waited {elapsed:.1f}s"
    assert f"PID {holder_pid}" in raised.holder, raised.holder
    assert "holder unknown" not in raised.holder, raised.holder
    assert f"(PID {os.getpid()})" not in raised.holder, (
        f"named itself as the holder: {raised.holder}"
    )

    text = rec.text()
    assert "competing holder:" in text, text
    assert f"PID {holder_pid}" in text, text
    assert "command line" in raised.holder, (
        f"holder description should carry the command line: {raised.holder}"
    )
    # Not just the label: the resolved command line must be there. The holder is a
    # live process owned by this user, so "unavailable" here means the resolution is
    # broken — which is exactly what this test exists to catch.
    assert str(script) in raised.holder, (
        f"holder command line was not resolved: {raised.holder}"
    )


def test_free_database_opens_without_waiting():
    """No lock, no wait: the budget must not turn into an unconditional sleep."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db_path = Path(td) / "free.duckdb"
        started = time.monotonic()
        con = loader._open_connection(db_path, budget_s=30)
        elapsed = time.monotonic() - started
        con.close()
    assert elapsed < 5, f"uncontended open took {elapsed:.1f}s"


def test_parses_the_holder_from_the_production_message():
    """Pin the exact text captured from data/logs/ingest.log on 2026-11-22."""
    named = (
        '_duckdb.IOException: IO Error: Cannot open file '
        '"\\\\?\\C:\\Users\\siddh\\Code_Library\\gdelt-events\\data\\gdelt.duckdb": '
        'The process cannot access the file because it is being used by another '
        'process.\n\nFile is already open in \nC:\\Python313\\python.exe (PID 7864)'
    )
    exe, pid = loader.holder_from_error(Exception(named))
    assert pid == 7864, (exe, pid)
    assert exe and exe.endswith("python.exe"), (exe, pid)
    assert loader.is_lock_conflict(Exception(named))

    # The Windows message on its own (no holder line) must not invent a PID.
    bare = Exception(
        'IO Error: Cannot open file "x.duckdb": The process cannot access the file '
        'because it is being used by another process.'
    )
    assert loader.holder_from_error(bare) == (None, None)
    assert loader.is_lock_conflict(bare)

    # Something that is not contention must not be treated as a lock wait.
    other = Exception("IO Error: No space left on device")
    assert loader.holder_from_error(other) == (None, None)
    assert not loader.is_lock_conflict(other)


def test_bulk_paths_default_to_the_measured_budget():
    """Backfills carry the measured budget, not the old ~82 s one."""
    assert loader.BULK_LOCK_WAIT_BUDGET_S >= 1760, (
        "bulk budget must cover the longest measured reader hold (1760 s, 2026-11-22)"
    )
    assert loader.LOCK_WAIT_BUDGET_S < loader.BULK_LOCK_WAIT_BUDGET_S, (
        "short-lived writers keep the old patience so they cannot stretch a cycle"
    )


def test_ingest_lock_budget_fits_the_task_window():
    """The ingest's wait must never reach GDELT-Ingest's ExecutionTimeLimit (PT10M).

    A wait that overruns the task limit is killed by Task Scheduler mid-wait: no load,
    no `Ingest failed` line, a terminated task result. That is worse than the failure
    this change set out to remove, so the property is pinned here.
    """
    from pipeline import gdelt_ingest as ingest

    assert ingest.INGEST_TASK_TIME_LIMIT_S == 600.0, "must match deploy/tasks/GDELT-Ingest.xml"
    assert ingest.INGEST_CYCLE_TARGET_S < ingest.INGEST_TASK_TIME_LIMIT_S, (
        "the cycle must aim to finish before the hard kill, not at it"
    )
    assert ingest.INGEST_LOCK_RESERVE_S >= 240, (
        "reserve must cover the work that follows the wait (measured max cycle 386 s)"
    )

    # A fresh cycle gets the wanted budget; a late one gets only what is left; a
    # cycle with no headroom gets none at all (it defers immediately).
    assert ingest.ingest_lock_budget(0.0, now=0.0) == ingest.INGEST_LOCK_WAIT_WANT_S
    assert ingest.ingest_lock_budget(0.0, now=10.0) == ingest.INGEST_LOCK_WAIT_WANT_S
    assert ingest.ingest_lock_budget(0.0, now=61.0) == 239.0
    assert ingest.ingest_lock_budget(0.0, now=200.0) == 100.0
    assert ingest.ingest_lock_budget(0.0, now=500.0) == 0.0
    assert ingest.ingest_lock_budget(0.0, now=10_000.0) == 0.0

    # The invariant: while there is headroom, elapsed + wait + reserve stays inside the
    # cycle target; once there is none, the cycle waits zero seconds and defers at once.
    for elapsed in (0, 30, 60, 120, 240, 300, 420, 540, 700):
        budget = ingest.ingest_lock_budget(0.0, now=float(elapsed))
        headroom = ingest.INGEST_CYCLE_TARGET_S - ingest.INGEST_LOCK_RESERVE_S - elapsed
        if headroom > 0:
            total = elapsed + budget + ingest.INGEST_LOCK_RESERVE_S
            assert total <= ingest.INGEST_CYCLE_TARGET_S + 1e-9, (
                f"elapsed {elapsed}s + wait {budget}s + reserve exceeds the cycle target"
            )
        else:
            assert budget == 0.0, f"waited {budget}s with no headroom left at {elapsed}s"

    assert ingest.DEFER_EXIT_CODE != 0, "a deferral must not look like success (#42)"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            started = time.monotonic()
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - standalone runner
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"pass {name} ({time.monotonic() - started:.1f}s)")
    sys.exit(1 if failures else 0)
