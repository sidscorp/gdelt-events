"""Content-asserting health check for the incremental pipeline.

Run by the `GDELT-PipelineCheck` task every 15 minutes. Process-alive checks do not
catch the failures this pipeline actually has: the 2026-07 embedder outage ran for
eleven days with every batch failing, and the 2026-11 feed-cache hole served HTTP 200
with no article cards. Both looked healthy from the outside. So every check below
asserts on *content*, not on whether a process exists or a command exited zero.

Exit 0 = every check passed. Exit 1 = at least one failed, which Task Scheduler
records in LastTaskResult and the wrapper appends to data/logs/pipeline_check.log.

The one failure this script repairs is Ollama: restarting `Ollama-Serve` is a single,
safe, idempotent action and its absence breaks embedding, semantic search and the
gateway's local model aliases. Everything else is reported, never "fixed" by guessing.
"""
import argparse
import json
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8015",
                    help="dashboard base URL (default: the production instance)")
    ap.add_argument("--quiet", action="store_true", help="only print failures and the summary")
    args = ap.parse_args()

    results: list[tuple[str, str, str]] = []
    check_ollama(results)
    check_feed(results, args.base)
    check_ingest(results)
    check_embed_run(results)
    check_backlog(results)

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
