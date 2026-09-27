"""Review-first Bluesky publishing for GDELT Monitor event signals.

The worker is intentionally independent from the ingest/cluster writers:
DuckDB is opened read-only, all workflow state lives in SQLite, and at most one
post can be published per run.  With no Bluesky credentials it still builds the
review queue and reports setup status without borrowing personal credentials.

Usage:
    python pipeline/social_publisher.py --dry-run
    python pipeline/social_publisher.py
    python pipeline/social_publisher.py --calibrate
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dashboard"))

from importance import compute_importance  # noqa: E402
from social_store import (  # noqa: E402
    begin_run, count_created_today, expire_stale, finish_run, get_social_db,
    init_social_db, mark_publish_error, mark_published, mark_publishing,
    next_approved, publication_counts, recent_titles, set_setting, setting,
    upsert_candidate, update_post_text,
)
from webutil import usable_title  # noqa: E402

try:
    from .config import DATA_DIR, DB_PATH, LOG_DIR
except ImportError:
    from config import DATA_DIR, DB_PATH, LOG_DIR


LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG = logging.getLogger("gdelt.social")
if not LOG.handlers:
    LOG.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "social_publisher.log", maxBytes=3 * 1024 * 1024,
        backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.addHandler(logging.StreamHandler())
    LOG.propagate = False


CANON_BASE = "https://gdeltmonitor.com"
PILOT_TOPICS = {
    "cyber_attacks": ("cyber-attacks", "Cybersecurity"),
    "supply_chain": ("supply-chain-alerts", "Supply Chain"),
    "fda_agency": ("fda-agency", "FDA"),
    "public_health": ("public-health", "Public Health"),
}
TOPIC_ORDER = {cat: i for i, cat in enumerate(PILOT_TOPICS)}

MIN_SOURCES = 3
# A single judge-approved member assigns the topic to its near-duplicate event
# cluster. Requiring two independently tagged variants sounded safer, but a
# production replay showed it suppressed every fresh candidate: pill scoring
# does not necessarily visit every syndicated copy, while clustering already
# establishes that the copies describe the same event.
MIN_TOPIC_MEMBERS = 1
MAX_EVENT_AGE_H = 6
MAX_EVENT_LIFE_H = 24
MAX_QUEUE_PER_DAY = 10
MAX_POSTS_24H = 5
MAX_TOPIC_POSTS_24H = 2
MIN_POST_GAP_MIN = 90
POST_LIMIT = 300
EXPLANATION_LIMIT = 150
LOCK_FILE = DATA_DIR / ".social_publisher.lock"
SECRETS_FILE = DATA_DIR / ".bluesky_bot"
GATEWAY_KEY_FILE = DATA_DIR / ".social_gateway_key"
GATEWAY_URL = os.environ.get(
    "GDELT_SOCIAL_GATEWAY_URL", "https://llm.snambiar.com/v1/chat/completions"
)
SOCIAL_MODEL = os.environ.get(
    "GDELT_SOCIAL_MODEL", "accounts/fireworks/models/gpt-oss-120b"
)
BSKY_SERVICE = os.environ.get("BLUESKY_SERVICE", "https://bsky.social")

_WORD = re.compile(r"[a-z0-9]+")
_NUMBER = re.compile(r"(?<!\w)\d[\d,.%/-]*")
_BANNED = re.compile(
    r"\b(will|likely|could|may|might|expected to|set to|signals? that|"
    r"underscores?|highlights?|pressure|impact markets?|investors?|stocks?|"
    r"buy|sell|recommend|forecast|outlook)\b",
    re.I,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_from_gdelt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(int(value)).zfill(14), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _title_key(title: str) -> str:
    return " ".join(_WORD.findall((title or "").lower()))[:500]


def _near_duplicate(title_key: str, old_key: str) -> bool:
    a, b = set(title_key.split()), set(old_key.split())
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.78


def _source_identity(member: dict) -> str:
    domain = (member.get("domain") or "").lower().strip()
    if not domain and member.get("url"):
        domain = (urllib.parse.urlsplit(member["url"]).hostname or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or (member.get("outlet") or member.get("url") or "").lower().strip()


def _source_count(members: list[dict]) -> int:
    return len({s for s in (_source_identity(m) for m in members) if s})


def _span_hours(first_seen, latest_seen) -> float:
    first, latest = _dt_from_gdelt(first_seen), _dt_from_gdelt(latest_seen)
    if not first or not latest:
        return 0.0
    return round(max(0.0, (latest - first).total_seconds() / 3600), 1)


def _span_label(hours: float) -> str:
    if hours < 1:
        return "<1h"
    if hours < 24:
        return f"{max(1, round(hours))}h"
    return f"{max(1, round(hours / 24))}d"


def _trim_words(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 1)].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[: limit - 1]).rstrip() + "…"


def factual_explanation(title: str) -> str:
    clean = re.sub(r"\s+", " ", title or "").strip().rstrip(". ")
    if not clean:
        return "Multiple sources are reporting the same developing event."
    return _trim_words(clean + ".", EXPLANATION_LIMIT)


def build_post_text(candidate: dict, explanation: str | None = None) -> str:
    explanation = _trim_words(
        explanation or factual_explanation(candidate.get("title", "")),
        EXPLANATION_LIMIT,
    )
    topic = candidate["topic_name"].upper()
    header = (
        f"{topic} · {candidate['source_count']} sources · "
        f"{_span_label(float(candidate['coverage_span_hours']))}"
    )
    url = candidate["event_url"]
    fixed = f"{header}\n\n\n\nEvidence: {url}"
    available = POST_LIMIT - len(fixed)
    explanation = _trim_words(explanation, min(EXPLANATION_LIMIT, max(40, available)))
    text = f"{header}\n\n{explanation}\n\nEvidence: {url}"
    if len(text) > POST_LIMIT:
        explanation = _trim_words(explanation, max(20, len(explanation) - (len(text) - POST_LIMIT)))
        text = f"{header}\n\n{explanation}\n\nEvidence: {url}"
    return text


def _read_secret_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _gateway_key() -> str | None:
    return os.environ.get("GDELT_SOCIAL_GATEWAY_KEY") or (
        GATEWAY_KEY_FILE.read_text(encoding="utf-8").strip()
        if GATEWAY_KEY_FILE.exists() else None
    )


def _json_request(url: str, payload: dict, *, token: str | None = None,
                  timeout: int = 60) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _gateway_call(prompt: str, max_tokens: int = 1200) -> str:
    key = _gateway_key()
    if not key:
        raise RuntimeError("dedicated social gateway key is not configured")
    body = _json_request(
        GATEWAY_URL,
        {
            "model": SOCIAL_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "reasoning_effort": "low",
        },
        token=key,
        timeout=90,
    )
    return ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")


def _json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model response")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response is not an object")
    return value


def _source_lines(members: list[dict]) -> tuple[str, dict[int, dict]]:
    selected: list[dict] = []
    seen: set[str] = set()
    for member in sorted(members, key=lambda m: m.get("crawled_at") or 0, reverse=True):
        identity = _source_identity(member)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        selected.append(member)
        if len(selected) >= 12:
            break
    by_n = {i + 1: m for i, m in enumerate(selected)}
    lines = []
    for i, member in by_n.items():
        text = f"{i}. [{member.get('outlet') or member.get('domain') or 'source'}] "
        text += (member.get("title") or "")[:220]
        if member.get("desc"):
            text += " — " + member["desc"][:300]
        lines.append(text)
    return "\n".join(lines), by_n


def _deterministic_grounding(explanation: str, source_text: str) -> tuple[bool, str]:
    if not explanation or len(explanation) > EXPLANATION_LIMIT:
        return False, "missing or too long"
    if _BANNED.search(explanation):
        return False, "predictive, advisory, or causal language"
    source_lower = source_text.lower()
    for number in _NUMBER.findall(explanation):
        if number.lower() not in source_lower:
            return False, f"number absent from evidence: {number}"
    if len(explanation.split()) < 5:
        return False, "too little context"
    return True, "deterministic checks passed"


def grounded_explanation(candidate: dict) -> tuple[str, str, dict]:
    """Return explanation, kind and an inspectable grounding record."""
    fallback = factual_explanation(candidate["title"])
    source_text, by_n = _source_lines(candidate["members"])
    if not _gateway_key() or not source_text:
        return fallback, "facts", {"supported": False, "reason": "generator unavailable"}
    prompt = (
        "Write the text for an automated public-interest news signal. Use ONLY the "
        "numbered source snippets below. Write one or two plain-language sentences, "
        f"at most {EXPLANATION_LIMIT} characters total. State what happened, then why "
        "it matters only when that consequence is explicitly stated by a source. "
        "Do not predict, recommend, infer market effects, add causality, or add any fact "
        "or number absent from the snippets. Neutral language only.\n\n"
        "Output ONLY JSON: "
        '{"explanation":"...","support":[1,2]}\n\nSources:\n' + source_text
    )
    try:
        generated = _json_object(_gateway_call(prompt))
        explanation = re.sub(r"\s+", " ", str(generated.get("explanation") or "")).strip()
        support = sorted({int(n) for n in generated.get("support", []) if str(n).isdigit()})
        ok, reason = _deterministic_grounding(explanation, source_text)
        if not ok or not support or any(n not in by_n for n in support):
            return fallback, "facts", {"supported": False, "reason": reason, "support": support}
        verify_prompt = (
            "Determine whether every factual and causal claim in EXPLANATION is directly "
            "supported by at least one SOURCE. Reject implications, predictions, advice, "
            "and plausible-but-unstated background knowledge. Output ONLY JSON: "
            '{"supported":true|false,"reason":"<12 words>"}\n\n'
            f"EXPLANATION: {explanation}\n\nSOURCES:\n{source_text}"
        )
        verdict = _json_object(_gateway_call(verify_prompt, max_tokens=800))
        supported = verdict.get("supported") is True
        grounding = {
            "supported": supported,
            "reason": str(verdict.get("reason") or "")[:200],
            "support": support,
            "model": SOCIAL_MODEL,
        }
        return (explanation, "generated", grounding) if supported else (fallback, "facts", grounding)
    except Exception as exc:
        LOG.warning("social explanation fell back to facts: %s", exc)
        return fallback, "facts", {"supported": False, "reason": str(exc)[:200]}


def _connect_news_db():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        con.execute("SET memory_limit='1000MB'")
        con.execute("SET threads=2")
        con.execute(f"SET temp_directory='{(DATA_DIR / 'duckdb_tmp').as_posix()}'")
    except Exception:
        pass
    return con


def scan_candidates() -> list[dict]:
    latest_cutoff = int((_utcnow() - timedelta(hours=MAX_EVENT_AGE_H)).strftime("%Y%m%d%H%M%S"))
    first_cutoff = int((_utcnow() - timedelta(hours=MAX_EVENT_LIFE_H)).strftime("%Y%m%d%H%M%S"))
    placeholders = ",".join("?" for _ in PILOT_TOPICS)
    query = f"""
        WITH tagged AS (
            SELECT cm.cluster_id, t.category, count(DISTINCT t.article_id) AS tag_members
            FROM cluster_members cm
            JOIN article_tags t ON t.article_id=cm.article_url AND t.source_type='gal'
            WHERE t.category IN ({placeholders})
            GROUP BY cm.cluster_id, t.category
        )
        SELECT c.cluster_id, c.title, c.first_seen, c.latest_seen, c.members_json,
               tagged.category, tagged.tag_members
        FROM clusters c JOIN tagged USING(cluster_id)
        WHERE c.status='active' AND c.latest_seen >= ? AND c.first_seen >= ?
        ORDER BY c.latest_seen DESC
        LIMIT 600
    """
    con = _connect_news_db()
    try:
        rows = con.execute(
            query, [*PILOT_TOPICS.keys(), latest_cutoff, first_cutoff]
        ).fetchall()
    finally:
        con.close()

    grouped: dict[str, dict] = {}
    for cid, title, first_seen, latest_seen, members_json, category, tag_members in rows:
        if not usable_title(title) or int(tag_members or 0) < MIN_TOPIC_MEMBERS:
            continue
        try:
            members = json.loads(members_json or "[]")
        except json.JSONDecodeError:
            continue
        sources = _source_count(members)
        if sources < MIN_SOURCES:
            continue
        item = grouped.setdefault(cid, {
            "cluster_id": cid,
            "title": re.sub(r"\s+", " ", title or "").strip(),
            "title_key": _title_key(title),
            "first_seen": first_seen,
            "latest_seen": latest_seen,
            "members": members,
            "source_count": sources,
            "n_sources": sources,
            "coverage_span_hours": _span_hours(first_seen, latest_seen),
            "topics": [],
        })
        item["topics"].append((category, int(tag_members or 0)))

    candidates = list(grouped.values())
    compute_importance(candidates)
    for item in candidates:
        category, tag_members = sorted(
            item.pop("topics"), key=lambda pair: (-pair[1], TOPIC_ORDER[pair[0]])
        )[0]
        topic_id, topic_name = PILOT_TOPICS[category]
        item.update({
            "topic_id": topic_id,
            "topic_name": topic_name,
            "member_tags": tag_members,
            "importance_score": float(item.pop("_imp", 0)),
            "event_url": f"{CANON_BASE}/event/{item['cluster_id']}?src=bsky",
        })
    return candidates


def _passes_auto_threshold(candidate: dict) -> bool:
    try:
        thresholds = json.loads(setting("auto_thresholds", "{}") or "{}")
    except json.JSONDecodeError:
        return False
    rule = thresholds.get(candidate["topic_id"])
    if not rule:
        return False
    return (
        candidate["source_count"] >= int(rule.get("min_sources", 999999))
        and candidate["importance_score"] >= float(rule.get("min_score", 2.0))
    )


def queue_candidates(dry_run: bool = False) -> tuple[int, int]:
    candidates = scan_candidates()
    old_titles = recent_titles(48)
    capacity = max(0, MAX_QUEUE_PER_DAY - count_created_today())
    queued = 0
    mode = setting("mode", "review")
    for candidate in candidates:
        if queued >= capacity:
            break
        if any(_near_duplicate(candidate["title_key"], old) for _, old in old_titles):
            continue
        explanation, kind, grounding = grounded_explanation(candidate)
        candidate.update({
            "explanation": explanation,
            "explanation_kind": kind,
            "grounding": grounding,
            "status": "approved" if mode == "auto" and _passes_auto_threshold(candidate) else "pending",
            "expires_at": (_utcnow() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
        })
        candidate["post_text"] = build_post_text(candidate, explanation)
        if dry_run:
            LOG.info("dry-run candidate %s: %s", candidate["cluster_id"], candidate["post_text"])
            queued += 1
            continue
        if upsert_candidate(candidate):
            queued += 1
            old_titles.append((candidate["cluster_id"], candidate["title_key"]))
    return len(candidates), queued


def _credentials() -> dict[str, str]:
    values = _read_secret_file(SECRETS_FILE)
    return {
        "handle": os.environ.get("BLUESKY_HANDLE") or values.get("BLUESKY_HANDLE", ""),
        "password": os.environ.get("BLUESKY_APP_PASSWORD") or values.get("BLUESKY_APP_PASSWORD", ""),
    }


def _bsky_session(creds: dict[str, str]) -> dict:
    return _json_request(
        f"{BSKY_SERVICE}/xrpc/com.atproto.server.createSession",
        {"identifier": creds["handle"], "password": creds["password"]},
        timeout=30,
    )


def _upload_blob(access_jwt: str, image_path: Path) -> dict | None:
    if not image_path.exists():
        return None
    req = urllib.request.Request(
        f"{BSKY_SERVICE}/xrpc/com.atproto.repo.uploadBlob",
        data=image_path.read_bytes(),
        headers={"Content-Type": "image/png", "Authorization": f"Bearer {access_jwt}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", "replace")).get("blob")
    except Exception as exc:
        LOG.warning("Bluesky thumbnail upload failed; publishing link card without it: %s", exc)
        return None


def _link_facet(text: str, url: str) -> dict:
    start = text.index(url)
    return {
        "index": {
            "byteStart": len(text[:start].encode("utf-8")),
            "byteEnd": len(text[: start + len(url)].encode("utf-8")),
        },
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
    }


def _record_key(cluster_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._~-]", "-", cluster_id)
    return f"evt-{safe}"[:120]


def publish_candidate(candidate: dict) -> tuple[str, str | None]:
    creds = _credentials()
    if not creds["handle"] or not creds["password"]:
        raise RuntimeError("Bluesky bot credentials are not configured")
    post_text = build_post_text(candidate, candidate.get("explanation"))
    update_post_text(candidate["cluster_id"], post_text)
    session = _bsky_session(creds)
    access = session["accessJwt"]
    did = session["did"]
    rkey = _record_key(candidate["cluster_id"])
    mark_publishing(candidate, rkey, post_text)

    external = {
        "$type": "app.bsky.embed.external",
        "external": {
            "uri": candidate["event_url"],
            "title": _trim_words(candidate["title"], 300),
            "description": _trim_words(
                f"{candidate['topic_name']} · {candidate['source_count']} sources · "
                f"{_span_label(float(candidate['coverage_span_hours']))}. "
                f"{candidate.get('explanation') or ''}",
                300,
            ),
        },
    }
    blob = _upload_blob(access, REPO / "dashboard" / "static" / "og-card.png")
    if blob:
        external["external"]["thumb"] = blob
    record = {
        "$type": "app.bsky.feed.post",
        "text": post_text,
        "createdAt": _utcnow().isoformat().replace("+00:00", "Z"),
        "langs": ["en"],
        "facets": [_link_facet(post_text, candidate["event_url"])],
        "embed": external,
    }
    try:
        response = _json_request(
            f"{BSKY_SERVICE}/xrpc/com.atproto.repo.createRecord",
            {
                "repo": did,
                "collection": "app.bsky.feed.post",
                "rkey": rkey,
                "validate": True,
                "record": record,
            },
            token=access,
            timeout=30,
        )
        return response["uri"], response.get("cid")
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        query = urllib.parse.urlencode({
            "repo": did, "collection": "app.bsky.feed.post", "rkey": rkey,
        })
        req = urllib.request.Request(
            f"{BSKY_SERVICE}/xrpc/com.atproto.repo.getRecord?{query}",
            headers={"Authorization": f"Bearer {access}"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            existing = json.loads(response.read().decode("utf-8", "replace"))
        return existing["uri"], existing.get("cid")


def publish_one(dry_run: bool = False) -> int:
    candidate = next_approved()
    if not candidate:
        return 0
    total, topic, latest = publication_counts(candidate["topic_id"])
    if total >= MAX_POSTS_24H or topic >= MAX_TOPIC_POSTS_24H:
        return 0
    if latest:
        try:
            last_dt = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if (_utcnow() - last_dt).total_seconds() < MIN_POST_GAP_MIN * 60:
                return 0
        except ValueError:
            pass
    if dry_run:
        LOG.info("dry-run would publish %s", candidate["cluster_id"])
        return 0
    try:
        uri, cid = publish_candidate(candidate)
        mark_published(candidate["cluster_id"], uri, cid)
        LOG.info("published %s as %s", candidate["cluster_id"], uri)
        return 1
    except Exception as exc:
        mark_publish_error(candidate["cluster_id"], str(exc))
        raise


def calibrate() -> tuple[bool, dict]:
    """Evaluate reviewed pilot labels; unlock auto only on a chronological holdout."""
    con = get_social_db()
    rows = [dict(r) for r in con.execute(
        "SELECT topic_id, source_count, importance_score, review_decision, "
        "explanation_kind, rejection_reason, created_at FROM social_candidates "
        "WHERE review_decision IN ('approve','reject') ORDER BY created_at"
    ).fetchall()]
    con.close()
    report: dict = {"reviewed": len(rows), "ready": False, "thresholds": {}}
    if len(rows) < 50:
        report["reason"] = "at least 50 reviewed candidates are required"
        return False, report
    split = max(1, int(len(rows) * 0.8))
    train, holdout = rows[:split], rows[split:]
    if len(holdout) < 10:
        report["reason"] = "chronological holdout has fewer than 10 candidates"
        return False, report

    thresholds = {}
    selected_holdout = []
    for topic_id, _ in ((v[0], v[1]) for v in PILOT_TOPICS.values()):
        topic_train = [r for r in train if r["topic_id"] == topic_id]
        best = None
        source_values = sorted({int(r["source_count"]) for r in topic_train})
        score_values = sorted({round(float(r["importance_score"]), 1) for r in topic_train})
        for min_sources in source_values:
            for min_score in score_values:
                chosen = [r for r in topic_train if r["source_count"] >= min_sources
                          and r["importance_score"] >= min_score]
                if len(chosen) < 3:
                    continue
                precision = sum(r["review_decision"] == "approve" for r in chosen) / len(chosen)
                if precision >= 0.9 and (best is None or len(chosen) > best[0]):
                    best = (len(chosen), min_sources, min_score, precision)
        if best is None:
            report["reason"] = f"no 90% precision rule for {topic_id}"
            return False, report
        _, min_sources, min_score, precision = best
        thresholds[topic_id] = {
            "min_sources": min_sources, "min_score": min_score,
            "train_precision": round(precision, 3),
        }
        selected_holdout.extend(
            r for r in holdout if r["topic_id"] == topic_id
            and r["source_count"] >= min_sources and r["importance_score"] >= min_score
        )
    if not selected_holdout:
        report["reason"] = "thresholds selected no holdout candidates"
        return False, report
    holdout_precision = sum(
        r["review_decision"] == "approve" for r in selected_holdout
    ) / len(selected_holdout)
    edited = sum(r["explanation_kind"] == "edited" for r in rows if r["review_decision"] == "approve")
    approved = sum(r["review_decision"] == "approve" for r in rows)
    edit_rate = edited / approved if approved else 1.0
    unsupported_recent = any(
        r["review_decision"] == "reject"
        and re.search(r"unsupported|ungrounded|false|incorrect", r["rejection_reason"] or "", re.I)
        and r["created_at"] >= (_utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        for r in rows
    )
    report.update({
        "thresholds": thresholds,
        "holdout_selected": len(selected_holdout),
        "holdout_precision": round(holdout_precision, 3),
        "material_edit_rate": round(edit_rate, 3),
        "unsupported_rejection_in_last_7d": unsupported_recent,
    })
    ready = holdout_precision >= 0.9 and edit_rate <= 0.05 and not unsupported_recent
    report["ready"] = ready
    if ready:
        set_setting("auto_thresholds", json.dumps(thresholds, sort_keys=True))
        set_setting("auto_ready", "1")
    else:
        set_setting("auto_ready", "0")
        report["reason"] = "holdout precision, edit-rate, or seven-day grounding gate failed"
    return ready, report


def _acquire_lock() -> int | None:
    try:
        return os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age > 60 * 60:
                LOCK_FILE.unlink()
                return os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            pass
        return None


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    finally:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    args = parser.parse_args()
    init_social_db()
    if args.calibrate:
        ready, report = calibrate()
        print(json.dumps(report, indent=2, sort_keys=True))
        print(("HEALTHY" if ready else "PROBLEMS") + ": social calibration "
              + ("passed" if ready else "not ready"))
        return 0 if ready else 1

    fd = _acquire_lock()
    if fd is None:
        print("HEALTHY: social publisher already running; skipped overlap")
        return 0
    run_id = begin_run()
    scanned = queued = published = 0
    try:
        expire_stale()
        mode = setting("mode", "review")
        if mode != "off":
            scanned, queued = queue_candidates(dry_run=args.dry_run)
            if not args.dry_run:
                published = publish_one()
        finish_run(run_id, "ok", scanned, queued, published)
        setup = _credentials()
        suffix = ""
        if not setup["handle"] or not setup["password"]:
            suffix = "; Bluesky credentials pending"
        print(
            f"HEALTHY: social mode={mode} scanned={scanned} queued={queued} "
            f"published={published}{suffix}"
        )
        return 0
    except Exception as exc:
        LOG.exception("social publisher failed")
        finish_run(run_id, "error", scanned, queued, published, str(exc))
        print(f"PROBLEMS: social publisher failed: {str(exc)[:240]}")
        return 1
    finally:
        _release_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
