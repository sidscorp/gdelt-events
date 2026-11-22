"""Content-asserting health check for the incremental pipeline.

Run by the `GDELT-PipelineCheck` task every 15 minutes. Process-alive checks do not
catch the failures this pipeline actually has: the 2026-07 embedder outage ran for
eleven days with every batch failing, the 2026-11 feed-cache hole served HTTP 200
with no article cards, and ingestion itself was dead for 69 hours (2026-11-16 ->
11-19) while every check returned success. All of them looked healthy from the
outside. So every check below asserts on *content*, not on whether a process exists
or a command exited zero.

The freshness check is the one that answers "is data still arriving?" It reads
`max(gal.crawled_at)` -- the newest row the corpus actually holds -- rather than the
mtime of a file that a stuck-but-running process can keep touching. Its verdict is
FAIL when the newest row is more than FRESH_MAX_AGE_MIN old.

Exit 0 = every check passed. Exit 1 = at least one failed. The wrapper appends the
output to data/logs/pipeline_check.log; note that `LastTaskResult` does NOT currently
carry this exit code, because gdelt_pipeline_check_hidden.vbs launches the check with
`WScript.Shell.Run cmd, 0, False` (fire-and-forget) and exits 0 immediately. Treat the
log as the record until that wrapper is fixed.

The one failure this script repairs is Ollama: restarting `Ollama-Serve` is a single,
safe, idempotent action and its absence breaks embedding, semantic search and the
gateway's local model aliases. Everything else is reported, never "fixed" by guessing.
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_TASK = "Ollama-Serve"
INGEST_MAX_AGE_MIN = 20      # ingest runs every 15 min
EMBED_LOG_MAX_AGE_MIN = 45   # embed task runs every 15 min
BACKLOG_WINDOW_H = 2         # the incremental embedder looks back 2h
# The incremental embedder drains in 15-minute batches (a normal cycle finds and
# embeds ~650), so a few hundred unembedded at any instant is expected between runs.
# The signal that matters is whether the backlog is DRAINING, so the check compares
# consecutive samples and only fires on sustained growth or a multi-cycle ceiling.
BACKLOG_CEILING = 2000       # ~3 normal cycles piled up
BACKLOG_HISTORY = 3          # consecutive increases before it counts as growth
BACKLOG_STATE = DATA / ".pipeline_backlog.json"
FEED_MIN_CARDS = 1

# Freshness: is the corpus still growing? GDELT publishes GAL every ~15 minutes, so
# an hour with no new row is a stall, not jitter. 60 minutes is the issue-42 bound.
FRESH_MAX_AGE_MIN = 60
FRESH_DB_RETRIES = 3         # ingest holds the write lock for part of each cycle
FRESH_DB_RETRY_SLEEP = 5     # seconds between read-only connection attempts
# Deliberate stops (rebuilds, backfills, prep_rebuild.ps1) are announced by this file
# rather than by an edit to the check. Its first line is the reason, and it is reported
# as WARN -- a paused monitor must never look like a passing one.
FRESH_PAUSE_FILE = DATA / ".freshness_pause"
# GDELT_ALERT_DIR keeps a verification run (or a dev instance) from writing a synthetic
# alert into the production alert path; the same idea as config.GDELT_DATA_DIR.
ALERT_DIR = Path(os.environ.get("GDELT_ALERT_DIR") or (DATA / "alerts"))
FRESH_ALERT_FILE = ALERT_DIR / "freshness.json"


def _get(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "gdelt-pipeline-check/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _age_minutes(path: Path) -> float | None:
    try:
        return (time.time() - path.stat().st_mtime) / 60.0
    except OSError:
        return None


def check_ollama(results: list) -> None:
    """Alive *and* serving a model list. Restarts the task once if it is not."""
    for attempt in (1, 2):
        try:
            status, body = _get(OLLAMA_URL, timeout=5)
            models = json.loads(body).get("models", [])
            if status == 200 and models:
                results.append(("PASS", "ollama", f"{len(models)} models"))
                return
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            pass
        if attempt == 1:
            results.append(("WARN", "ollama", "not answering; restarting Ollama-Serve"))
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Start-ScheduledTask -TaskName {OLLAMA_TASK}"],
                    capture_output=True, timeout=60,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(20)
    results.append(("FAIL", "ollama", "still not answering after restart"))


def check_feed(results: list, base: str) -> None:
    try:
        status, body = _get(base.rstrip("/") + "/", timeout=15)
    except (urllib.error.URLError, OSError) as exc:
        results.append(("FAIL", "feed", f"unreachable: {exc}"))
        return
    cards = body.count('class="article"')
    if status != 200:
        results.append(("FAIL", "feed", f"HTTP {status}"))
    elif cards < FEED_MIN_CARDS:
        results.append(("FAIL", "feed", f"HTTP 200 but {cards} article cards (shell served)"))
    else:
        results.append(("PASS", "feed", f"{cards} article cards"))


def check_ingest(results: list) -> None:
    age = _age_minutes(DATA / "data_version.txt")
    if age is None:
        results.append(("FAIL", "ingest", "data_version.txt missing"))
    elif age > INGEST_MAX_AGE_MIN:
        results.append(("FAIL", "ingest", f"last completed cycle {age:.0f} min ago "
                                          f"(limit {INGEST_MAX_AGE_MIN})"))
    else:
        results.append(("PASS", "ingest", f"last cycle {age:.0f} min ago"))


def _parse_gdelt_ts(value) -> datetime | None:
    """Accept what the corpus actually stores: YYYYMMDDHHMMSS, as int or str.

    `gal.crawled_at` is a BIGINT of GDELT's own UTC stamp (e.g. 20261122173200),
    not a TIMESTAMP -- comparing it to `now()` needs an explicit cast, and DuckDB
    will refuse. Parsing it here keeps the conversion in one place.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def evaluate_freshness(newest, now=None, max_age_min: int = FRESH_MAX_AGE_MIN) -> tuple[str, str]:
    """Decide the freshness verdict from the newest `gal.crawled_at`.

    Split out from the database read so the rule can be exercised against a fixture
    without a live corpus, the same reason `evaluate_backlog` exists. Ingestion was
    dead for 69 hours in Nov 2026 with every check green; the rule is only worth
    wiring up if it is known to fire on a corpus that stopped growing.
    """
    now = now or datetime.now(timezone.utc)
    if newest is None:
        return "FAIL", "gal holds no rows at all"
    ts = _parse_gdelt_ts(newest)
    if ts is None:
        return "FAIL", f"unreadable crawled_at {newest!r}"
    age = (now - ts).total_seconds() / 60.0
    stamp = ts.strftime("%Y-%m-%d %H:%M:%SZ")
    if age < -5:
        # crawled_at is GDELT's UTC stamp, so a row in the future means this host's
        # clock is behind and every age computed here is wrong. Not a pass.
        return "FAIL", (f"newest row is {-age:.0f} min in the future ({stamp}); "
                        f"host clock is behind and ages cannot be trusted")
    if age > max_age_min:
        return "FAIL", (f"newest article {age:.0f} min old (limit {max_age_min}); "
                        f"last row {stamp}")
    return "PASS", f"newest article {age:.0f} min old ({stamp})"


def _newest_crawled_at(db: Path) -> tuple[object, str | None]:
    """`max(gal.crawled_at)`, or (None, why it could not be read).

    Read-only, and with `temp_directory` set: without it a spill on this connection
    fails `\\.tmp invalid` and poisons every later query on it (see PLAN-100X §5).
    The ingest task holds the write lock for part of each cycle, so a busy database
    is retried rather than reported -- a WARN here, never a false freshness FAIL.
    """
    import duckdb

    err = "unknown error"
    for attempt in range(FRESH_DB_RETRIES):
        con = None
        try:
            con = duckdb.connect(str(db), read_only=True)
            con.execute(f"SET temp_directory='{(db.parent / 'duckdb_tmp').as_posix()}'")
            row = con.execute("SELECT max(crawled_at) FROM gal").fetchone()
            return (row[0] if row else None), None
        except Exception as exc:  # DuckDB raises IOException on a held file
            err = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:120]
            if attempt + 1 < FRESH_DB_RETRIES:
                time.sleep(FRESH_DB_RETRY_SLEEP)
        finally:
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass
    return None, err


def _freshness_pause_reason() -> str | None:
    """Reason from data/.freshness_pause, or None when the gate should run."""
    try:
        text = FRESH_PAUSE_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    reason = " ".join(text.split())
    return (reason or "no reason recorded")[:160]


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_freshness_alert() -> dict:
    try:
        data = json.loads(FRESH_ALERT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_freshness_alert(record: dict) -> None:
    try:
        FRESH_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FRESH_ALERT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, FRESH_ALERT_FILE)
    except OSError as exc:
        logging.getLogger("pipeline_health").error(
            "could not write %s: %s", FRESH_ALERT_FILE, exc)


def notify_freshness_unhealthy(newest, detail: str, now=None) -> bool:
    """THE delivery point for a freshness failure. Currently: log ERROR + write a file.

    Deliberately one function with one call site, because there is no alert channel in
    this repo that this host can reach without inventing a credential:

      * `social_store.list_pending_notify()` is Bluesky-candidate specific and is polled
        by beardy. Writing a fake candidate into it would corrupt that pipeline's state.
      * beardy/Matrix has no HTTP ingress. Sending a message needs beardy's own Matrix
        credentials, which live on snambiar-linux, not on rainbow-boi.
      * the `~/news` feed on snambiar-linux IS reachable from here (rainbow-boi's
        existing ssh key, BatchMode verified 2026-11-22) but it is a pull-based change
        log that beardy reads on demand -- a record, not a push alert.

    So the failure is logged at ERROR and written to data/alerts/freshness.json, shaped
    like the payload a sender would need (state, what, measured age, limit, first
    failure, consecutive count) so adding a channel does not mean re-deriving the facts.
    Returns True only if a message was actually delivered; nothing delivers yet.
    """
    now = now or datetime.now(timezone.utc)
    log = logging.getLogger("pipeline_health")
    previous = _read_freshness_alert()
    was_unhealthy = previous.get("state") == "unhealthy"
    ts = _parse_gdelt_ts(newest)
    record = {
        "state": "unhealthy",
        "check": "freshness",
        "what": "GDELT ingestion has stopped: gal.crawled_at is not advancing",
        "detail": detail,
        "newest_crawled_at": None if newest is None else str(newest),
        "age_minutes": None if ts is None else round((now - ts).total_seconds() / 60.0, 1),
        "limit_minutes": FRESH_MAX_AGE_MIN,
        "first_failed_at": (previous.get("first_failed_at") if was_unhealthy else _iso(now)),
        "checked_at": _iso(now),
        "consecutive_failures": (int(previous.get("consecutive_failures") or 0) + 1
                                 if was_unhealthy else 1),
        "delivered": False,
    }
    _write_freshness_alert(record)
    log.error("FRESHNESS FAILURE: %s -- no alert channel configured; "
              "record written to %s", detail, FRESH_ALERT_FILE)
    return False


def _mark_freshness_recovered(detail: str, now: datetime) -> None:
    """Turn a stale unhealthy record into a recovered one, keeping the failure facts."""
    previous = _read_freshness_alert()
    if previous.get("state") != "unhealthy":
        return
    record = dict(previous)
    record.update({
        "state": "recovered",
        "recovered_at": _iso(now),
        "recovered_detail": detail,
        "consecutive_failures": previous.get("consecutive_failures"),
    })
    _write_freshness_alert(record)
    logging.getLogger("pipeline_health").info(
        "freshness recovered: %s (was failing since %s)",
        detail, previous.get("first_failed_at"))


def check_freshness(results: list, db_path: Path | str | None = None) -> None:
    """Is the corpus still growing? The check the 69-hour outage needed.

    `db_path` exists so the rule can be pointed at a fixture in tests; production
    always reads data/gdelt.duckdb.
    """
    db = Path(db_path) if db_path is not None else DATA / "gdelt.duckdb"
    if not db.exists():
        results.append(("WARN", "freshness", "database not present on this host"))
        return
    pause = _freshness_pause_reason()
    if pause:
        results.append(("WARN", "freshness", f"paused by {FRESH_PAUSE_FILE.name}: {pause}"))
        return
    newest, err = _newest_crawled_at(db)
    if err is not None:
        # A database held by the ingest writer is normal; a held-and-stuck one is
        # covered by check_ingest (data_version.txt), which cannot be masked this way.
        results.append(("WARN", "freshness", f"skipped: {err}"))
        return
    level, detail = evaluate_freshness(newest)
    results.append((level, "freshness", detail))
    if level == "FAIL":
        notify_freshness_unhealthy(newest, detail)
    else:
        _mark_freshness_recovered(detail, datetime.now(timezone.utc))


def check_embed_run(results: list) -> None:
    age = _age_minutes(DATA / "logs" / "embed_new.log")
    if age is None:
        results.append(("WARN", "embed-log", "log not found"))
    elif age > EMBED_LOG_MAX_AGE_MIN:
        results.append(("FAIL", "embed-log", f"no embed run in {age:.0f} min "
                                             f"(limit {EMBED_LOG_MAX_AGE_MIN})"))
    else:
        results.append(("PASS", "embed-log", f"last run {age:.0f} min ago"))


def _read_backlog_history() -> list[int]:
    try:
        return [int(x) for x in json.loads(BACKLOG_STATE.read_text())][-BACKLOG_HISTORY:]
    except (OSError, ValueError, TypeError):
        return []


def _write_backlog_history(values: list[int]) -> None:
    try:
        BACKLOG_STATE.parent.mkdir(parents=True, exist_ok=True)
        BACKLOG_STATE.write_text(json.dumps(values))
    except OSError:
        pass


def evaluate_backlog(n: int, history: list[int]) -> tuple[str, str]:
    """Decide the backlog verdict from one sample plus prior samples.

    Split out from check_backlog so the rule can be tested without a database: the
    check is only worth running if it is known to fire on a real stall.
    """
    history = (list(history) + [n])[-BACKLOG_HISTORY:]
    trend = " -> ".join(str(x) for x in history)
    grew = (len(history) >= BACKLOG_HISTORY
            and all(b > a for a, b in zip(history, history[1:])))
    if n >= BACKLOG_CEILING + 1:
        return "FAIL", (f"backlog at the {BACKLOG_CEILING} ceiling "
                        f"({n}+ unembedded in {BACKLOG_WINDOW_H}h; trend {trend})")
    if grew:
        return "FAIL", f"backlog growing across {BACKLOG_HISTORY} checks: {trend}"
    return "PASS", f"{n} unembedded in {BACKLOG_WINDOW_H}h (trend {trend})"


def check_backlog(results: list) -> None:
    """Recent articles still missing an embedding — the metric the old watchdog lacked.

    A running embedder that fails every batch looks identical to a healthy one from
    the outside; the backlog is what actually tells you it is working.
    """
    try:
        import duckdb
        try:
            from pipeline.embed_new_articles import find_missing_urls
        except ImportError:  # run as a plain script rather than -m
            from embed_new_articles import find_missing_urls
    except Exception as exc:  # pragma: no cover - import environment only
        results.append(("WARN", "embed-backlog", f"import failed: {exc}"))
        return
    db = DATA / "gdelt.duckdb"
    if not db.exists():
        results.append(("WARN", "embed-backlog", "database not present on this host"))
        return
    con = None
    try:
        con = duckdb.connect(str(db), read_only=True)
        missing = find_missing_urls(con, BACKLOG_CEILING + 1, hours_back=BACKLOG_WINDOW_H)
    except Exception as exc:
        # Doppler/busy database during an ingest cycle is normal, not a failure.
        results.append(("WARN", "embed-backlog", f"skipped: {str(exc)[:80]}"))
        return
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass
    n = len(missing)
    history = _read_backlog_history()
    level, detail = evaluate_backlog(n, history)
    _write_backlog_history((history + [n])[-BACKLOG_HISTORY:])
    results.append((level, "embed-backlog", detail))


# Name -> check. `--only freshness --db <fixture>` is how the gate is mutation-tested
# without the live corpus or the dashboard being involved.
CHECKS = {
    "ollama": lambda r, a: check_ollama(r),
    "feed": lambda r, a: check_feed(r, a.base),
    "ingest": lambda r, a: check_ingest(r),
    "freshness": lambda r, a: check_freshness(r, db_path=a.db),
    "embed-log": lambda r, a: check_embed_run(r),
    "embed-backlog": lambda r, a: check_backlog(r),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8015",
                    help="dashboard base URL (default: the production instance)")
    ap.add_argument("--db", default=None,
                    help="DuckDB file for the freshness check (default: data/gdelt.duckdb)")
    ap.add_argument("--only", default="",
                    help="comma-separated subset of checks to run: "
                         + ", ".join(CHECKS) + " (default: all)")
    ap.add_argument("--quiet", action="store_true", help="only print failures and the summary")
    args = ap.parse_args()

    wanted = [n.strip() for n in args.only.split(",") if n.strip()] or list(CHECKS)
    unknown = [n for n in wanted if n not in CHECKS]
    if unknown:
        ap.error(f"unknown check(s): {', '.join(unknown)}; known: {', '.join(CHECKS)}")

    results: list[tuple[str, str, str]] = []
    for name in wanted:
        CHECKS[name](results, args)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    failed = [r for r in results if r[0] == "FAIL"]
    for level, name, detail in results:
        if args.quiet and level == "PASS":
            continue
        print(f"{stamp} {level:4} {name:14} {detail}")
    print(f"{stamp} {'PROBLEMS' if failed else 'HEALTHY'}: "
          f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
