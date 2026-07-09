"""AI-briefing generation for the GDELT dashboard.

Self-contained briefing logic extracted from app.py (pure move). The Flask
route ``api_briefing`` stays in app.py and imports the needed symbols from
this module.
"""

import json
import re
import time
import logging
import threading
from datetime import datetime

from db import get_db, _hours_cutoff
from views import find_view
from _paths import OPENROUTER_KEY_PATH

# Same singleton logger app.py configures; getLogger by name returns it (no
# circular import on app).
req_log = logging.getLogger("dashboard.requests")


# Routed through the self-hosted LLM gateway (LiteLLM @ llm.snambiar.com). The key
# file now holds the gateway virtual key, and the model is a gateway alias.
OPENROUTER_URL = "https://llm.snambiar.com/v1/chat/completions"
BRIEFING_MODEL = "cerebras-fast"  # Cerebras GLM-4.7 (fast, no-thinking) via the gateway; if Cerebras 402s (credits), "neuralwatt-kimi" works as fallback
BRIEFING_TTL_S = 2700  # 45 minutes (pre-warmed for hot combos; news doesn't move that fast)
_OPENROUTER_KEY_PATH = OPENROUTER_KEY_PATH


def _get_openrouter_key():
    import os
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    if _OPENROUTER_KEY_PATH.exists():
        return _OPENROUTER_KEY_PATH.read_text().strip()
    return None


# --- Langfuse observability (optional; fully no-op if unconfigured) ---
# Keys live in <data>/.langfuse_key (gitignored), env-style:
#   LANGFUSE_PUBLIC_KEY=pk-lf-...
#   LANGFUSE_SECRET_KEY=sk-lf-...
#   LANGFUSE_HOST=https://langfuse.snambiar.com
LANGFUSE_KEY_PATH = _OPENROUTER_KEY_PATH.parent / ".langfuse_key"
_langfuse = None
_langfuse_checked = False


def _get_langfuse():
    """Lazy-init the Langfuse client. Returns None (and never raises) when
    credentials are absent or the SDK fails to initialize."""
    global _langfuse, _langfuse_checked
    if _langfuse_checked:
        return _langfuse
    _langfuse_checked = True
    try:
        import os
        if LANGFUSE_KEY_PATH.exists():
            for line in LANGFUSE_KEY_PATH.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        from langfuse import Langfuse
        lf = Langfuse()
        if getattr(lf, "_tracing_enabled", False):
            _langfuse = lf
            req_log.info("Langfuse tracing enabled (host=%s)", os.environ.get("LANGFUSE_HOST"))
        else:
            req_log.info("Langfuse tracing disabled (no credentials)")
    except Exception:
        req_log.debug("Langfuse init failed", exc_info=True)
    return _langfuse


PREV_BRIEFING_MAX_AGE_S = 48 * 3600   # older than this = not a useful anchor
PREV_BRIEFING_MAX_CHARS = 1500


def _age_label(generated_at: str) -> tuple[float, str]:
    """(age_seconds, human label like 'about 3 hours ago') for a UTC timestamp."""
    age_s = (datetime.utcnow() - datetime.strptime(generated_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
    if age_s < 90 * 60:
        label = f"about {max(1, int(age_s // 60))} minutes ago"
    elif age_s < 24 * 3600:
        label = f"about {int(round(age_s / 3600))} hours ago"
    elif age_s < 48 * 3600:
        label = "yesterday"
    else:
        label = f"{int(age_s // 86400)} days ago"
    return age_s, label


def _build_briefing_prompt(sources: list[dict], view_name: str, view_desc: str,
                            hours: int, prev: dict | None = None,
                            threads: list[dict] | None = None) -> str:
    """Build a time-aware, context-rich briefing prompt with numbered sources
    the model can cite by index. ``prev`` (the last cached briefing for this
    view/window) and ``threads`` (persistent storylines) make the briefing
    continue from prior coverage instead of restarting from scratch."""
    # Previous-briefing continuity: only if recent enough to be a useful anchor.
    prev_block = ""
    prev_links = set()
    if prev and prev.get("briefing") and prev.get("generated_at"):
        try:
            age_s, age_label = _age_label(prev["generated_at"])
        except (ValueError, TypeError):
            age_s = None
        if age_s is not None and age_s < PREV_BRIEFING_MAX_AGE_S:
            prev_links = {s.get("link") for s in (prev.get("sources") or []) if s.get("link")}
            # A compact "previously covered" TITLE LIST, not the previous
            # briefing's full prose: embedding the whole prior narrative
            # anchored the model so hard it omitted brand-new top stories
            # (2026-07-09 Platner incident) even against explicit instructions.
            prev_titles = [
                f"- {(s.get('title') or '')[:110]}"
                for s in (prev.get("sources") or [])[:8] if s.get("title")
            ]
            prev_block = (
                f"CONTINUITY: You last briefed this reader {age_label}. Stories you "
                f"already covered then (do NOT re-explain their background — describe "
                f"only what moved, escalated, or resolved; if one has gone quiet, say "
                f"so in one short line):\n" + "\n".join(prev_titles) + "\n"
                f"Stories marked NEW in the source list were not covered before — a "
                f"heavily-covered NEW story always outranks continuing coverage.\n\n"
            )

    threads_block = ""
    if threads:
        lines = []
        for t in threads[:8]:
            if (t.get("status") or "active") != "active":
                continue
            lines.append(f"- {t.get('title')} (tracking since {t.get('first_seen')}): {t.get('summary')}")
        if lines:
            threads_block = (
                "ONGOING STORY THREADS you have been tracking across briefings (use these "
                "to convey progression — e.g. 'day 4 of…', 'the third such incident this "
                "week' — and note when one resolves):\n" + "\n".join(lines) + "\n\n"
            )

    def _fmt(s):
        outlet = s.get("outlet") or "source"
        n = s.get("n_sources") or 1
        tag = f"{outlet} · {n} sources" if n > 1 else outlet
        if prev_links and s.get("link") not in prev_links:
            tag = f"NEW · {tag}"
        line = f"[{tag}] {s.get('title') or ''}"
        desc = (s.get("description") or "").strip()
        if desc:
            line += f" — {desc}"
        return line

    numbered = "\n".join(f"{i+1}. {_fmt(s)}" for i, s in enumerate(sources))

    # Human-readable time range
    if hours <= 1:
        time_label = "the last hour"
    elif hours <= 24:
        time_label = f"the last {hours} hours"
    elif hours <= 72:
        time_label = f"the last {hours // 24} days"
    elif hours <= 168:
        time_label = "the last week"
    else:
        time_label = f"the last {hours // 24} days"

    topic_context = ""
    if view_name and view_name != "Global News":
        topic_context = (
            f'This feed monitors "{view_name}" — {view_desc}. '
            f"Focus your analysis on developments relevant to this topic. "
        )

    return (
        f"You are a senior news intelligence analyst writing a briefing for a decision-maker. "
        f"Below are {len(sources)} numbered news stories from {time_label}, drawn from "
        f"44,000+ global sources monitored in real-time and de-duplicated into distinct events "
        f"(a story covered by many outlets shows a 'N sources' count and is a single numbered item).\n"
        f"The list is RANKED BY IMPORTANCE (coverage + momentum + recency): low-numbered items "
        f"are the biggest stories right now. Every story in the top 5 must be represented in "
        f"the briefing — by a highlight of its own, or a one-line mention if it truly warrants "
        f"less — regardless of whether it fits the prior narrative.\n\n"
        f"{topic_context}"
        f"{prev_block}"
        f"{threads_block}"
        f"Write a structured intelligence briefing in this format:\n\n"
        f"Start with a markdown H2 header line (begins with '## ') stating the topic and time window "
        f"(e.g., '## AI & Machine Learning — Last 24 Hours' or '## Global News — Last 7 Days'). "
        f"The topic is \"{view_name}\" and the time window is {time_label}.\n\n"
        f"Then write a 2-4 sentence executive summary that LEADS with the single most "
        f"consequential new development — a specific event, actor, and stake — not a survey. "
        f"Never open with panoramic filler like 'The global landscape is dominated by…' or "
        f"'The last {hours} hours have been marked by…'.\n\n"
        f"Then provide 5-8 key highlights as a markdown bullet list (use '- ' for each bullet). "
        f"Each highlight should be a clear, specific sentence naming concrete companies, countries, "
        f"people, or figures. Focus on what changed, what's escalating, what was announced, "
        f"and what a strategist should watch.\n\n"
        f"End with a single sentence on what to watch next.\n\n"
        f"CITATIONS: After each highlight (and any specific factual claim), cite the supporting "
        f"story/stories using bracketed numbers that match the numbered list below, e.g. '[3]' or "
        f"'[3][7]'. Each number is one story even if covered by many outlets — cite it once, not per "
        f"outlet. Cite only stories that directly support the claim; aim for 1-2 citations per "
        f"highlight. Use only numbers from the list — never invent numbers, and never write URLs.\n\n"
        f"COVERAGE DUTY: the numbered list is ranked by importance — every story in the "
        f"top 5 MUST appear in the briefing (as a highlight, or a one-line mention), even "
        f"if it does not fit the prior narrative.\n\n"
        f"Be specific and concrete. No filler. No hedging. "
        f"Use markdown formatting for emphasis and structure.\n\n"
        f"Numbered sources:\n{numbered}"
    )


def _generate_briefing_stream(sources, view_name, view_desc, hours, prev=None, threads=None, prompt=None):
    """Stream briefing tokens from OpenRouter as SSE.

    When Langfuse is configured, the full generation (model, token usage, cost,
    latency, view/hours) is logged once the stream completes."""
    import time
    from urllib.request import Request, urlopen

    key = _get_openrouter_key()
    if not key:
        return

    if prompt is None:
        prompt = _build_briefing_prompt(sources, view_name, view_desc, hours, prev=prev, threads=threads)
    payload = json.dumps({
        "model": BRIEFING_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8000,  # headroom for reasoning models (e.g. neuralwatt fallback); GLM stops naturally well under this
        "temperature": 0.3,
        "stream": True,
        "stream_options": {"include_usage": True},  # OpenAI-style: usage in the final stream chunk
    }).encode()

    req = Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )

    lf = _get_langfuse()
    t0 = time.monotonic()
    collected = []
    usage = None
    try:
        with urlopen(req, timeout=120) as resp:
            for line in resp:
                # 'replace' so a chunk-boundary split can never crash (and truncate)
                # the stream; malformed events are skipped by the json guard below.
                line = line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        collected.append(content)
                        yield content
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue
    finally:
        if lf:
            try:
                meta = {
                    "view": view_name,
                    "hours": hours,
                    "n_sources": len(sources),
                    "latency_s": round(time.monotonic() - t0, 2),
                }
                with lf.start_as_current_observation(
                    name="gdelt-briefing",
                    as_type="generation",
                    model=BRIEFING_MODEL,
                    input=f"[{view_name} / {hours}h] {len(sources)} de-duplicated sources",
                    output="".join(collected),
                    metadata=meta,
                ) as gen:
                    if usage:
                        try:
                            gen.update(usage_details={
                                "input": usage.get("prompt_tokens"),
                                "output": usage.get("completion_tokens"),
                                "total": usage.get("total_tokens"),
                            })
                            if usage.get("cost") is not None:
                                gen.update(cost_details={"total": usage.get("cost")})
                        except Exception:
                            pass
            except Exception:
                req_log.debug("Langfuse briefing trace failed", exc_info=True)


def _generate_briefing(sources, view_name, view_desc, hours, prev=None, threads=None, prompt=None):
    """Non-streaming briefing generation (for caching)."""
    parts = []
    try:
        for chunk in _generate_briefing_stream(sources, view_name, view_desc, hours, prev=prev, threads=threads, prompt=prompt):
            parts.append(chunk)
    except Exception as e:
        req_log.warning("Briefing generation failed: %s", e)
        return None
    return "".join(parts).strip() if parts else None


BRIEFING_EVENT_BUFFER = 400   # articles pulled before dedup (pills/filtered)
BRIEFING_EVENT_LIMIT = 50     # distinct events fed to the model
BRIEFING_DESC_CHARS = 220


def _cluster_ids(con, urls):
    """url -> cluster_id for any of urls that are cluster members (chunked, guarded)."""
    out = {}
    try:
        for i in range(0, len(urls), 400):
            chunk = urls[i:i + 400]
            ph = ",".join(["?"] * len(chunk))
            for u, cid in con.execute(
                f"SELECT article_url, cluster_id FROM cluster_members WHERE article_url IN ({ph})",
                chunk,
            ).fetchall():
                out[u] = cid
    except Exception:
        pass  # cluster_members absent -> all singletons
    return out


def _cluster_sizes(con, cids):
    sizes = {}
    cids = [c for c in cids if c]
    try:
        for i in range(0, len(cids), 400):
            chunk = cids[i:i + 400]
            ph = ",".join(["?"] * len(chunk))
            for cid, size in con.execute(
                f"SELECT cluster_id, size FROM clusters WHERE cluster_id IN ({ph})", chunk,
            ).fetchall():
                sizes[cid] = size
    except Exception:
        pass
    return sizes


def _cluster_meta(con, cids):
    """cid -> (size, first_seen, latest_seen) for importance scoring. Chunked and
    guarded (returns {} if the clusters table is absent)."""
    meta = {}
    cids = [c for c in cids if c]
    try:
        for i in range(0, len(cids), 400):
            chunk = cids[i:i + 400]
            ph = ",".join(["?"] * len(chunk))
            for cid, size, fs, ls in con.execute(
                f"SELECT cluster_id, size, first_seen, latest_seen "
                f"FROM clusters WHERE cluster_id IN ({ph})", chunk,
            ).fetchall():
                meta[cid] = (size, fs, ls)
    except Exception:
        pass
    return meta


def _rep_from_members(mjson):
    """(outlet, description) from a cluster's denormalized members snapshot."""
    try:
        members = json.loads(mjson) if mjson else []
        if members:
            m = members[0]
            return m.get("outlet"), (m.get("desc") or "")[:BRIEFING_DESC_CHARS]
    except Exception:
        pass
    return None, ""


def _fetch_briefing_events(view_id, hours):
    """Fetch the window's most-IMPORTANT events for a briefing — the *same*
    ranked set the feed's Importance sort shows (importance = coverage +
    velocity + recency; see importance.py), so the AI summary reflects the top
    cards for the selected pill/timeslice rather than a differently-ranked list.

    Returns (events, view_name, view_desc), where each event has title,
    description, outlet, n_sources, and a 'link' to its event page (all outlets)
    or the article."""
    # Lazy import: articles.py imports the cluster helpers from this module, so a
    # module-level import here would create a cycle. By request time both are loaded.
    from articles import _window_events

    con = get_db()
    if con is None:
        return [], "Global News", ""

    events = []
    view_name, view_desc = "Global News", "All articles from 44K+ sources worldwide"
    try:
        cutoff = _hours_cutoff(hours) if hours else _hours_cutoff(168)
        view = find_view(view_id) if view_id else None
        if view:
            view_name = view["name"]
            view_desc = view.get("description", "") or view_desc

        for c in _window_events(con, view, cutoff)[:BRIEFING_EVENT_LIMIT]:
            title = (c.get("title") or "").strip()
            if len(title) <= 10:
                continue
            events.append({
                "title": title,
                "description": (c.get("description") or "")[:BRIEFING_DESC_CHARS],
                "outlet": c.get("outlet_name") or c.get("source"),
                "n_sources": c.get("n_sources", 1),
                "link": c.get("event_url") or c.get("url"),
            })
    except Exception:
        events = []
    finally:
        try:
            con.close()
        except Exception:
            pass

    return events, view_name, view_desc


# ---------------------------------------------------------------------------
# Story threads — persistent per-view storylines so briefings can continue
# ("day 4 of…") instead of restarting. Maintained by a small follow-up LLM
# call after each generation; failures never affect the briefing itself.
# ---------------------------------------------------------------------------

THREADS_MAX = 8
THREADS_STALE_DAYS = 7


def get_threads(cache_key: str) -> list[dict]:
    """Load active story threads for a view/window key. Never raises."""
    try:
        from models import get_user_db
        con = get_user_db()
        row = con.execute(
            "SELECT threads_json FROM briefing_threads WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        con.close()
        if row and row["threads_json"]:
            threads = json.loads(row["threads_json"])
            return threads if isinstance(threads, list) else []
    except Exception:
        req_log.debug("thread load failed for %s", cache_key, exc_info=True)
    return []


def _update_threads(cache_key: str, old_threads: list[dict], briefing_text: str):
    from urllib.request import Request, urlopen

    key = _get_openrouter_key()
    if not key:
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    prompt = (
        f"You maintain a compact list of ongoing news story threads for a briefing "
        f"service. Today is {today}.\n\n"
        f"CURRENT THREADS (JSON):\n{json.dumps(old_threads, indent=None)}\n\n"
        f"LATEST BRIEFING:\n{re.sub(r'\[\d+\]', '', briefing_text)[:3000]}\n\n"
        f"Update the thread list:\n"
        f"- For threads the briefing progressed: advance 'last_update' to today and rewrite "
        f"'summary' (<=25 words, the latest development).\n"
        f"- ADD a new thread (first_seen = today) for each significant story in the briefing "
        f"likely to produce follow-up news: conflicts, disasters, investigations, elections, "
        f"policy fights, market-moving events. A typical briefing yields 3-6 threads; an "
        f"empty result is almost always wrong.\n"
        f"- Set status='resolved' for concluded stories; DROP resolved threads and threads "
        f"with no update in the last {THREADS_STALE_DAYS} days.\n"
        f"- Keep at most {THREADS_MAX} threads, most significant first.\n\n"
        f"Output ONLY a JSON array (no prose, no code fence). Each element: "
        f'{{"slug": "kebab-case-id", "title": "...", "first_seen": "YYYY-MM-DD", '
        f'"last_update": "YYYY-MM-DD", "summary": "...", "status": "active"}}. '
        f"Preserve 'slug' and 'first_seen' of existing threads exactly."
    )
    payload = json.dumps({
        "model": BRIEFING_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
        "temperature": 0.1,
    }).encode()
    req = Request(OPENROUTER_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    t0 = time.monotonic()
    with urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    # Tolerate a stray code fence despite instructions.
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array in thread-update response")
    threads = json.loads(text[start:end + 1])
    if not isinstance(threads, list):
        raise ValueError("thread-update response is not a list")
    threads = [t for t in threads if isinstance(t, dict) and t.get("title")][:THREADS_MAX]

    from models import get_user_db
    con = get_user_db()
    con.execute(
        "INSERT OR REPLACE INTO briefing_threads (cache_key, threads_json, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        (cache_key, json.dumps(threads)),
    )
    con.commit()
    con.close()

    lf = _get_langfuse()
    if lf:
        try:
            usage = body.get("usage") or {}
            with lf.start_as_current_observation(
                name="gdelt-thread-update", as_type="generation", model=BRIEFING_MODEL,
                input=f"[{cache_key}] {len(old_threads)} threads in",
                output=json.dumps(threads),
                metadata={"latency_s": round(time.monotonic() - t0, 2)},
            ) as gen:
                gen.update(usage_details={
                    "input": usage.get("prompt_tokens"),
                    "output": usage.get("completion_tokens"),
                    "total": usage.get("total_tokens"),
                })
        except Exception:
            req_log.debug("Langfuse thread-update trace failed", exc_info=True)


def update_threads_async(cache_key: str, old_threads: list[dict], briefing_text: str):
    """Fire-and-forget thread maintenance after a briefing is generated.
    Any failure leaves the previous threads untouched."""
    def _run():
        try:
            _update_threads(cache_key, old_threads, briefing_text)
        except Exception as e:
            req_log.warning("thread update failed for %s: %s", cache_key, e)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
