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
import hashlib
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
    approved_queue, begin_run, count_created_today, count_shadow_today, expire_stale,
    finish_run, get_social_db, init_social_db, mark_publish_error, mark_published,
    mark_publishing, publication_counts, publication_topic_counts, recent_titles,
    record_shadow, set_setting, setting, shadow_done, upsert_candidate,
    update_post_text,
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
    "geopolitics_conflict": ("geopolitics", "Geopolitics"),
    "energy_climate": ("energy-climate", "Energy & Climate"),
    "ai_regulation": ("ai-regulation", "AI Policy"),
    "semiconductors": ("semiconductors", "Semiconductors"),
    "ai_general": ("ai-general", "AI"),
    # 2026-11-13: all remaining curated categories (regions + long-tail topics).
    "europe": ("europe", "Europe"),
    "asia": ("asia", "Asia"),
    "us": ("us", "United States"),
    "medical_devices": ("medical-devices", "Medical Devices"),
    "ai_defense": ("ai-defense", "AI & Defense"),
    "ai_sector_impact": ("ai-sector-impact", "AI in Industry"),
    "oss_vulnerabilities": ("oss-vulnerabilities", "OSS Vulnerabilities"),
    "nih_news": ("nih-news", "NIH"),
    "cms_news": ("cms-news", "CMS"),
    "va_news": ("va-news", "VA"),
}
TOPIC_ORDER = {cat: i for i, cat in enumerate(PILOT_TOPICS)}

MIN_SOURCES = 3
# Region pills are broad ("anything from the US"), so they need wider
# multi-outlet corroboration than a topical pill before they earn a post.
# 2026-11-14: the `us` pill was filling its daily cap with travel delays and
# celebrity items sourced from 3 outlets.
REGION_TOPIC_IDS = frozenset({"us", "europe", "asia"})
REGION_MIN_SOURCES = 5
# A region pill covers a whole continent, so its daily share cannot be the same
# as one narrow subject's. 2026-11-14: with a flat 3/topic cap, a queue holding
# only `us` items went silent for hours behind a cap that was doing its job.
TOPIC_POST_CAPS = {"us": 5, "europe": 5, "asia": 5}
# A single judge-approved member assigns the topic to its near-duplicate event
# cluster. Requiring two independently tagged variants sounded safer, but a
# production replay showed it suppressed every fresh candidate: pill scoring
# does not necessarily visit every syndicated copy, while clustering already
# establishes that the copies describe the same event.
MIN_TOPIC_MEMBERS = 1
MAX_EVENT_AGE_H = 6
MAX_EVENT_LIFE_H = 24
MAX_QUEUE_PER_DAY = 20
MAX_POSTS_24H = 10
MAX_TOPIC_POSTS_24H = 3
MIN_POST_GAP_MIN = 5
POST_LIMIT = 300
EXPLANATION_LIMIT = 150
SHADOW_DAILY_CAP = 30  # hard cost guard; real volume is <= MAX_QUEUE_PER_DAY
SEARCH_URL = os.environ.get(
    "GDELT_SOCIAL_SEARCH_URL", "http://100.121.22.84:8889/search"
)
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


TITLE_LIMIT = 80
_CHROME_PIPE = re.compile(r"\s*\|[^|]{1,60}$")


def _norm_outlet(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def clean_member_title(title: str, outlet: str | None = None,
                       domain: str | None = None) -> str:
    """Strip outlet chrome from a picked member headline.

    Two rules only, corpus-driven (2026-11 dry sample):
    1. A trailing " | tail" is always chrome (iHeart " | News Radio 610",
       " | Talk Radio 98.3 WLAC", " | Teletica"). Up to two passes for
       "Title | Section | Outlet".
    2. A trailing " - tail" is stripped ONLY when tail matches the member's
       own outlet/domain — bare-dash stripping eats real content
       ("el Betis - Chelsea de ejemplo" must survive).
    """
    t = re.sub(r"\s+", " ", (title or "")).strip()
    for _ in range(2):
        stripped = _CHROME_PIPE.sub("", t).strip()
        if stripped == t:
            break
        t = stripped
    m = re.match(r"^(?P<body>.+?)\s+[-–—]\s+(?P<tail>[^-–—]{1,60})$", t)
    if m:
        tail_n = _norm_outlet(m.group("tail"))
        keys = {_norm_outlet(outlet), _norm_outlet(domain)} - {""}
        if any(k in tail_n or tail_n in k for k in keys):
            t = m.group("body").strip()
    return t


def _title_outlet(candidate: dict) -> tuple[str | None, str | None]:
    """Outlet/domain of the member that produced the cluster title."""
    raw = candidate.get("title") or ""
    for m in candidate.get("members") or []:
        if (m.get("title") or "") == raw:
            return m.get("outlet"), m.get("domain")
    first = (candidate.get("members") or [{}])[0]
    return first.get("outlet"), first.get("domain")


def _shouty(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    return len(letters) >= 10 and sum(c.isupper() for c in letters) / len(letters) > 0.7


def _title_grounding(title: str, explanation: str, source_text: str) -> tuple[bool, str]:
    """Deterministic title checks, same bar as _deterministic_grounding."""
    if not title:
        return False, "empty"
    if len(title) > TITLE_LIMIT:
        return False, f"over {TITLE_LIMIT} chars"
    if _shouty(title):
        return False, "all-caps wire style"
    if _BANNED.search(title):
        return False, "predictive or advisory language"
    haystack = (explanation + " " + source_text).lower()
    for number in _NUMBER.findall(title):
        if number.lower() not in haystack:
            return False, f"number absent from evidence: {number}"
    return True, "ok"


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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise urllib.error.HTTPError(
            exc.url, exc.code, f"{exc.msg}; response body: {body}",
            exc.headers, None,
        ) from None


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


def _draft_fields(raw: str) -> tuple[str, str, list[int]]:
    """Parse a draft/repair response into (title, explanation, support).

    Raises ValueError via _json_object when the model returns no JSON object,
    which is the failure that used to drop a whole candidate straight to the
    facts fallback on one bad response.
    """
    generated = _json_object(raw)
    title = re.sub(r"\s+", " ", str(generated.get("title") or "")).strip().rstrip(".!?")
    explanation = re.sub(r"\s+", " ", str(generated.get("explanation") or "")).strip()
    support = sorted({int(n) for n in generated.get("support", []) if str(n).isdigit()})
    return title, explanation, support


def grounded_explanation(candidate: dict) -> tuple[str, str, dict, str]:
    """Return explanation, kind, grounding record and the social card title.

    The title is drafted in the SAME call as the explanation (so the two cannot
    disagree), verified jointly against the source snippets, and falls back to
    the chrome-stripped member headline — a raw picked headline never reaches
    a card. The cluster title is untouched; only social_candidates.title
    carries the social title.
    """
    outlet, domain = _title_outlet(candidate)
    fallback_title = clean_member_title(candidate["title"], outlet, domain)
    fallback = factual_explanation(fallback_title)
    source_text, by_n = _source_lines(candidate["members"])
    if not _gateway_key() or not source_text:
        return fallback, "facts", {"supported": False, "reason": "generator unavailable"}, fallback_title
    prompt = (
        "Write the text for an automated public-interest news signal. Use ONLY the "
        "numbered source snippets below. Two outputs in one JSON object:\n"
        f'- "title": at most {TITLE_LIMIT} characters, sentence case, no outlet or '
        "publication names, no trailing punctuation. Name the event directly — what "
        "happened, where, who is affected — so a reader who sees only the title knows "
        "what the story is. No angle, no reaction framing, and only claims the "
        "explanation also makes.\n"
        f'- "explanation": one or two plain-language sentences, at most '
        f"{EXPLANATION_LIMIT} characters total. State what happened, then why it "
        "matters only when that consequence is explicitly stated by a source.\n"
        "Do not predict, recommend, infer market effects, add causality, or add any "
        "fact or number absent from the snippets. Neutral language only.\n\n"
        'Output ONLY JSON: {"title":"...","explanation":"...","support":[1,2]}\n\n'
        "Sources:\n" + source_text
    )
    try:
        try:
            title, explanation, support = _draft_fields(_gateway_call(prompt))
        except Exception as exc:
            # One stricter retry: a single unparseable response used to send the
            # candidate straight to the facts fallback (and a human ask) even
            # when the story was perfectly writable. 2026-11-14.
            LOG.info("social draft unparseable (%s); retrying once", exc)
            title, explanation, support = _draft_fields(
                _gateway_call(prompt + "\n\nReturn ONLY the JSON object: no prose, "
                                       "no markdown fence, nothing before or after it.")
            )
        ok, reason = _deterministic_grounding(explanation, source_text)
        repaired = False
        if not ok and explanation:
            # Repair pass: the deterministic gate rejects *wording* (predictions,
            # advice, causal framing, detached numbers) far more often than it
            # rejects facts, and each rejection became a human ask. Ask the model
            # to rewrite the same claims within the rules, then re-check. One
            # attempt only — it is cheaper than a review, and a second failure
            # still falls back to facts. 2026-11-14.
            rejected = reason
            repair_prompt = (
                "Rewrite this draft for an automated public-interest news signal so it "
                "passes strict verification. Keep only facts stated in the sources and "
                "add nothing new. Describe only what happened, in the past or present "
                "tense. If a detail can only be written with 'will', 'could', 'would', "
                "'expected', or a claim about what comes next, leave that detail out. "
                "Do not explain significance or use editorial verbs (underscores, "
                "highlights, signals, sets up). Do not compare or rank against other "
                "events. Only use a number if it appears verbatim in a source and is "
                "restated the same way. Never write about markets, investors, or advice. "
                "Write one or two complete sentences that name the subject explicitly — "
                "never begin a sentence with He, She, It, They, His, Her, or Their, and "
                "never leave a reader needing the original headline for context.\n"
                f"Rejected because: {rejected}\n\n"
                'Output ONLY JSON: {"title":"...","explanation":"...","support":[1,2]}\n\n'
                f"DRAFT TITLE: {title}\nDRAFT EXPLANATION: {explanation}\n\n"
                "Sources:\n" + source_text
            )
            try:
                r_title, r_explanation, r_support = _draft_fields(_gateway_call(repair_prompt))
                r_ok, r_reason = _deterministic_grounding(r_explanation, source_text)
                if r_ok:
                    title = r_title or title
                    explanation = r_explanation
                    support = r_support or support
                    ok, repaired = True, True
                    LOG.info("social explanation repaired (was: %s)", rejected)
                else:
                    explanation = r_explanation or explanation
            except Exception as exc:
                LOG.warning("social explanation repair failed: %s", exc)
        if not ok and explanation:
            # Deterministic salvage, no extra model call: the first sentence is
            # almost always the plain event statement, while the banned framing
            # ("...underscores a campaign...", "...will be the youngest...") sits
            # in the second. Keep the first sentence if it stands on its own.
            first_sentence = re.split(r"(?<=[.!?])\s+", explanation.strip())[0]
            if first_sentence and first_sentence != explanation:
                s_ok, _ = _deterministic_grounding(first_sentence, source_text)
                if s_ok:
                    explanation = first_sentence
                    ok, repaired = True, True
                    LOG.info("social explanation trimmed to its event sentence (was: %s)",
                             reason)
        tok, treason = _title_grounding(title, explanation, source_text)
        if not ok or not support or any(n not in by_n for n in support):
            return fallback, "facts", {"supported": False,
                                       "reason": f"{reason}; title: {treason}",
                                       "support": support}, fallback_title
        # Independent ladders: a rejected title alone must not sink a verified
        # explanation — the cleaned member headline fills in instead.
        ship_title = title if tok else fallback_title
        verify_prompt = (
            "Determine whether every factual and causal claim in TITLE and EXPLANATION "
            "is directly supported by at least one SOURCE. The title must not state or "
            "imply anything absent from the explanation. Reject implications, "
            "predictions, advice, and plausible-but-unstated background knowledge. "
            "Output ONLY JSON: "
            '{"supported":true|false,"reason":"<12 words>"}\n\n'
            f"TITLE: {ship_title}\nEXPLANATION: {explanation}\n\nSOURCES:\n{source_text}"
        )
        verdict = _json_object(_gateway_call(verify_prompt, max_tokens=800))
        supported = verdict.get("supported") is True
        grounding = {
            "supported": supported,
            "reason": str(verdict.get("reason") or "")[:200],
            "support": support,
            "model": SOCIAL_MODEL,
            "title_kind": "generated" if tok else "fallback",
            "repaired": repaired,
        }
        if supported:
            return explanation, "generated", grounding, ship_title
        return fallback, "facts", grounding, fallback_title
    except Exception as exc:
        LOG.warning("social explanation fell back to facts: %s", exc)
        return fallback, "facts", {"supported": False, "reason": str(exc)[:200]}, fallback_title


def _web_search(query: str, n: int = 5) -> list[dict]:
    """Self-hosted SearXNG (snambiar-linux); free, no key. Empty list on failure."""
    try:
        url = SEARCH_URL + "?" + urllib.parse.urlencode({"q": query, "format": "json"})
        with urllib.request.urlopen(url, timeout=15) as response:
            results = json.loads(response.read().decode("utf-8", "replace")).get("results") or []
        return [
            {"title": (r.get("title") or "")[:120], "content": (r.get("content") or "")[:250]}
            for r in results[:n]
        ]
    except Exception as exc:
        LOG.warning("shadow web search failed: %s", exc)
        return []


def shadow_verdict(candidate: dict) -> dict:
    """Shadow auto-approver: one search + one LLM call, ~$0.0003. Logs what an
    auto mode WOULD do with this candidate so it can be compared against human
    review before auto is ever enabled. Never touches candidate status."""
    source_text, _ = _source_lines(candidate["members"][:6])
    hits = _web_search(re.sub(r"\s+", " ", candidate["title"]).strip())
    web_text = "\n".join(
        f"{i}. {h['title']} — {h['content']}" for i, h in enumerate(hits, 1)
    ) or "none available"
    prompt = (
        "You are the pre-publication checker for an automated breaking-news account.\n\n"
        f"CANDIDATE HEADLINE: {candidate['title']}\n"
        f"PROPOSED POST SENTENCE: {candidate.get('explanation') or candidate['title']}\n\n"
        f"GDELT SOURCE SNIPPETS:\n{source_text}\n\n"
        f"INDEPENDENT WEB RESULTS:\n{web_text}\n\n"
        "Checks:\n"
        "1. CORROBORATED: sources and web results describe the same event the post "
        "sentence states. Fail if the event looks fabricated, badly dated, or about a "
        "different subject. If web results are absent, judge from the GDELT snippets "
        "alone and say so in the reason.\n"
        "2. NOT OVERSTATED: the post sentence must not assert as settled fact anything "
        "the evidence only alleges, links, estimates, or expects. Attribution "
        "('blamed on', 'linked to') must stay attributed.\n"
        "3. RELEVANT: a genuine, current, multi-source event worth a public signal.\n\n"
        'Output ONLY JSON: {"post": true|false, "reason": "<15 words>"}\n'
        "post=true only if all three checks pass."
    )
    verdict = _json_object(_gateway_call(prompt, max_tokens=400))
    return {
        "post": verdict.get("post") is True,
        "reason": str(verdict.get("reason") or "")[:200],
        "search_hits": len(hits),
    }


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
    kept: list[dict] = []
    for item in candidates:
        category, tag_members = sorted(
            item.pop("topics"), key=lambda pair: (-pair[1], TOPIC_ORDER[pair[0]])
        )[0]
        topic_id, topic_name = PILOT_TOPICS[category]
        # Region pills ("anything from the US") need wider corroboration before
        # they are worth posting; topical pills keep the normal bar. 2026-11-14.
        minimum = REGION_MIN_SOURCES if topic_id in REGION_TOPIC_IDS else MIN_SOURCES
        if item["source_count"] < minimum:
            continue
        item.update({
            "topic_id": topic_id,
            "topic_name": topic_name,
            "member_tags": tag_members,
            "importance_score": float(item.pop("_imp", 0)),
            "event_url": f"{CANON_BASE}/event/{item['cluster_id']}?src=bsky",
        })
        kept.append(item)
    return kept


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


def _auto_ok(candidate: dict, kind: str, grounding: dict, verdict: dict | None) -> bool:
    """Auto-mode approval bar (2026-11-06, Sidd relaxed the review gate).

    A candidate posts without human review only when ALL of these hold:
    the numeric per-topic threshold, a generated (not facts-fallback)
    explanation that passed joint title+body grounding verification, and a
    shadow approver verdict of would_post. Anything less stays pending for
    human review. The one documented miss this would have shipped is the
    Binance attribution overstatement (both gates passed it); wording matches
    the draft, not a human's edit.
    """
    return (
        _passes_auto_threshold(candidate)
        and kind == "generated"
        and grounding.get("supported") is True
        and verdict is not None
        and verdict["post"]
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
        explanation, kind, grounding, social_title = grounded_explanation(candidate)
        # Shadow verdict is computed BEFORE the status decision so auto mode can
        # gate on it; it is recorded after upsert (FK to social_candidates).
        verdict = None
        if not shadow_done(candidate["cluster_id"]) and count_shadow_today() < SHADOW_DAILY_CAP:
            try:
                verdict = shadow_verdict(candidate)
            except Exception as exc:
                LOG.warning("shadow verdict failed for %s: %s", candidate["cluster_id"], exc)
        auto = mode == "auto" and _auto_ok(candidate, kind, grounding, verdict)
        candidate.update({
            "explanation": explanation,
            "explanation_kind": kind,
            "grounding": grounding,
            "title": social_title,
            "status": "approved" if auto else "pending",
            "expires_at": (_utcnow() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
        })
        candidate["post_text"] = build_post_text(candidate, explanation)
        if dry_run:
            LOG.info("dry-run candidate %s [%s]: %s",
                     candidate["cluster_id"], candidate["status"], candidate["post_text"])
            queued += 1
            continue
        if upsert_candidate(candidate):
            queued += 1
            old_titles.append((candidate["cluster_id"], candidate["title_key"]))
        # Record the shadow verdict after upsert (FK). If upsert raises, the
        # verdict is recomputed next run — see 2026-11-03 lock incident.
        if verdict is not None:
            record_shadow(
                candidate["cluster_id"],
                "would_post" if verdict["post"] else "would_block",
                verdict["reason"], verdict["search_hits"], SOCIAL_MODEL,
            )
            LOG.info("shadow %s: %s (%s)",
                     candidate["cluster_id"],
                     "would_post" if verdict["post"] else "would_block",
                     verdict["reason"])
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


_TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"


def _tid_for(cluster_id: str, created_at: str) -> str:
    """Deterministic atproto TID rkey for a candidate.

    bsky.social rejects non-TID record keys on app.bsky.feed.post
    ("Invalid TID string format"). Deriving the TID from the candidate's
    created_at plus a hash of the cluster id keeps it stable across
    retries, so a retry after a lost response hits the 409 branch
    instead of posting a duplicate.
    """
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        micros = int(dt.timestamp() * 1_000_000)
    except (TypeError, ValueError):
        micros = int(time.time() * 1_000_000)
    clock_id = int.from_bytes(
        hashlib.sha256(cluster_id.encode("utf-8")).digest()[:2], "big"
    ) & 0x3FF
    value = (micros << 10) | clock_id
    return "".join(_TID_ALPHABET[(value >> shift) & 0x1F] for shift in range(60, -1, -5))


def publish_candidate(candidate: dict) -> tuple[str, str | None]:
    creds = _credentials()
    if not creds["handle"] or not creds["password"]:
        raise RuntimeError("Bluesky bot credentials are not configured")
    post_text = build_post_text(candidate, candidate.get("explanation"))
    update_post_text(candidate["cluster_id"], post_text)
    session = _bsky_session(creds)
    access = session["accessJwt"]
    did = session["did"]
    rkey = _tid_for(candidate["cluster_id"], candidate.get("created_at") or "")
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
    """Publish the best approved candidate whose topic still has room today.

    2026-11-14: this used to look at only the head of the approved queue and
    return 0 if that topic was capped, so one full topic (usually `us`, the
    broadest pill) blocked every other topic — the feed went silent for 17h
    while runs reported ok/published=0. Now the queue is walked and capped
    topics are skipped; the reason is logged instead of failing silently.
    Candidates Sidd approved by hand bypass the per-topic cap.
    """
    total, _, latest = publication_counts(None)
    if total >= MAX_POSTS_24H:
        LOG.info("publish skipped: %d posts in trailing 24h (cap %d)", total, MAX_POSTS_24H)
        return 0
    if latest:
        try:
            last_dt = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if (_utcnow() - last_dt).total_seconds() < MIN_POST_GAP_MIN * 60:
                return 0
        except ValueError:
            pass
    queue = approved_queue(50)
    if not queue:
        return 0
    topic_counts = publication_topic_counts()
    skipped: list[str] = []
    for candidate in queue:
        # A human approval outranks the per-topic throttle — Sidd reviewed it
        # deliberately and expects it to post. The 10/day total cap still binds.
        cap = TOPIC_POST_CAPS.get(candidate["topic_id"], MAX_TOPIC_POSTS_24H)
        if candidate.get("reviewed_by") is None and \
                topic_counts.get(candidate["topic_id"], 0) >= cap:
            skipped.append(candidate["topic_id"])
            continue
        if dry_run:
            LOG.info("dry-run would publish %s (%s)",
                     candidate["cluster_id"], candidate["topic_id"])
            return 0
        try:
            uri, cid = publish_candidate(candidate)
            mark_published(candidate["cluster_id"], uri, cid)
            LOG.info("published %s (%s) as %s",
                     candidate["cluster_id"], candidate["topic_id"], uri)
            return 1
        except Exception as exc:
            mark_publish_error(candidate["cluster_id"], str(exc))
            raise
    LOG.info("publish skipped: all %d approved candidates are at their per-topic cap (%s)",
             len(queue), ", ".join(sorted(set(skipped))))
    return 0


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
