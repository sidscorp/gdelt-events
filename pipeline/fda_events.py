"""FDA regulatory events ingestion via OpenFDA device API.

Polls FDA's public device endpoints every 6 hours and stores recalls,
510(k) clearances, and enforcement actions in `fda_regulatory_events`.

Endpoints used (free, no API key required for basic access):
  https://api.fda.gov/device/recall.json      — device recall actions
  https://api.fda.gov/device/510k.json        — 510(k) clearances
  https://api.fda.gov/device/enforcement.json — enforcement reports

Called from gdelt_ingest.py every 6 hours (checked by hour % 6 == 0).
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import DB_PATH
from .loader import _open_connection

log = logging.getLogger(__name__)

OPENFDA_BASE = "https://api.fda.gov/device"
REQUEST_TIMEOUT = 30
RESULTS_PER_PAGE = 100
MAX_PAGES = 5  # cap at 500 results per endpoint per run


def _fetch_json(url: str) -> dict | None:
    try:
        req = Request(url, headers={"User-Agent": "gdeltmonitor/1.0 (research)"})
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read())
    except URLError as e:
        log.warning("OpenFDA fetch failed: %s → %s", url, e)
        return None
    except Exception as e:
        log.warning("OpenFDA parse error: %s → %s", url, e)
        return None


def _date_to_int(date_str: str) -> int | None:
    """Convert YYYYMMDD or YYYY-MM-DD string to integer YYYYMMDD."""
    if not date_str:
        return None
    clean = date_str.replace("-", "")[:8]
    try:
        return int(clean) if len(clean) == 8 else None
    except ValueError:
        return None


def _fetch_recalls(days: int = 30) -> list[dict]:
    """Fetch recent device recall actions from OpenFDA."""
    # event_date_initiated is stored as YYYY-MM-DD in OpenFDA
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    until = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    events: list[dict] = []
    skip = 0
    for _ in range(MAX_PAGES):
        url = (
            f"{OPENFDA_BASE}/recall.json"
            f"?search=event_date_initiated:[{since}+TO+{until}]"
            f"&limit={RESULTS_PER_PAGE}&skip={skip}"
        )
        data = _fetch_json(url)
        if not data or "results" not in data:
            break
        for r in data["results"]:
            events.append({
                "event_id": r.get("res_event_number") or r.get("recall_number", ""),
                "event_type": "recall",
                "event_date": _date_to_int(r.get("event_date_initiated", "")),
                "firm_name": (r.get("recalling_firm") or "")[:300],
                "product_description": (r.get("product_description") or "")[:500],
                "recall_class": r.get("recall_class", ""),
                "reason_for_recall": (r.get("reason_for_recall") or "")[:500],
                "status": r.get("status", ""),
            })
        skip += RESULTS_PER_PAGE
        if len(data["results"]) < RESULTS_PER_PAGE:
            break
        time.sleep(0.3)
    return events


def _fetch_510k(days: int = 30) -> list[dict]:
    """Fetch recent 510(k) device clearances from OpenFDA."""
    # decision_date is stored as YYYY-MM-DD in OpenFDA
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    until = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    events: list[dict] = []
    skip = 0
    for _ in range(MAX_PAGES):
        url = (
            f"{OPENFDA_BASE}/510k.json"
            f"?search=decision_date:[{since}+TO+{until}]"
            f"&limit={RESULTS_PER_PAGE}&skip={skip}"
        )
        data = _fetch_json(url)
        if not data or "results" not in data:
            break
        for r in data["results"]:
            events.append({
                "event_id": r.get("k_number", ""),
                "event_type": "510k",
                "event_date": _date_to_int(r.get("decision_date", "")),
                "firm_name": (r.get("applicant") or "")[:300],
                "product_description": (r.get("device_name") or "")[:500],
                "recall_class": r.get("advisory_committee_description", ""),
                "reason_for_recall": r.get("decision_description", ""),
                "status": r.get("decision", ""),
            })
        skip += RESULTS_PER_PAGE
        if len(data["results"]) < RESULTS_PER_PAGE:
            break
        time.sleep(0.3)
    return events


def _fetch_enforcement(days: int = 30) -> list[dict]:
    """Fetch device enforcement reports from OpenFDA."""
    # report_date is stored as YYYYMMDD (no dashes) in OpenFDA enforcement
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y%m%d")
    until = (datetime.utcnow() + timedelta(days=1)).strftime("%Y%m%d")
    events: list[dict] = []
    skip = 0
    for _ in range(MAX_PAGES):
        url = (
            f"{OPENFDA_BASE}/enforcement.json"
            f"?search=report_date:[{since}+TO+{until}]"
            f"&limit={RESULTS_PER_PAGE}&skip={skip}"
        )
        data = _fetch_json(url)
        if not data or "results" not in data:
            break
        for r in data["results"]:
            events.append({
                "event_id": r.get("recall_number") or r.get("event_id", ""),
                "event_type": "enforcement",
                "event_date": _date_to_int(r.get("report_date", "")),
                "firm_name": (r.get("recalling_firm") or "")[:300],
                "product_description": (r.get("product_description") or "")[:500],
                "recall_class": r.get("classification", ""),
                "reason_for_recall": (r.get("reason_for_recall") or "")[:500],
                "status": r.get("status", ""),
            })
        skip += RESULTS_PER_PAGE
        if len(data["results"]) < RESULTS_PER_PAGE:
            break
        time.sleep(0.3)
    return events


def ingest_fda_events(db_path: Path | None = None, days: int = 30) -> dict:
    """Fetch and upsert recent FDA device regulatory events.

    Idempotent: uses INSERT OR REPLACE keyed on event_id. Safe to re-run.
    Called from gdelt_ingest.py at hours 0, 6, 12, 18 UTC.
    """
    db_path = db_path or DB_PATH
    con = _open_connection(db_path)
    now_ts = int(datetime.utcnow().strftime("%Y%m%d%H%M%S"))
    summary: dict[str, int] = {
        "recalls": 0, "clearances_510k": 0, "enforcement": 0, "errors": 0,
    }
    try:
        for fetch_fn, key in [
            (_fetch_recalls, "recalls"),
            (_fetch_510k, "clearances_510k"),
            (_fetch_enforcement, "enforcement"),
        ]:
            try:
                events = fetch_fn(days=days)
                rows = [
                    (
                        e["event_id"], e["event_type"], e["event_date"],
                        e["firm_name"], e["product_description"],
                        e["recall_class"], e["reason_for_recall"],
                        e["status"], now_ts,
                    )
                    for e in events
                    if e.get("event_id")
                ]
                if rows:
                    con.executemany(
                        "INSERT OR REPLACE INTO fda_regulatory_events "
                        "(event_id, event_type, event_date, firm_name, "
                        " product_description, recall_class, reason_for_recall, "
                        " status, fetched_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                summary[key] = len(rows)
            except Exception as e:
                log.warning("FDA events fetch failed for %s: %s", key, e)
                summary["errors"] += 1

        con.execute("CHECKPOINT")
    finally:
        con.close()

    log.info("FDA events ingested: %s", summary)
    return summary
