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
from webutil import usable_title
from _paths import OPENROUTER_KEY_PATH

# Same singleton logger app.py configures; getLogger by name returns it (no
# circular import on app).
req_log = logging.getLogger("dashboard.requests")


# Routed through the self-hosted LLM gateway (LiteLLM @ llm.snambiar.com). The key
# file now holds the gateway virtual key, and the model is a gateway alias.
OPENROUTER_URL = "https://llm.snambiar.com/v1/chat/completions"
# Fireworks gpt-oss-120b ($0.15/$0.60 per 1M) — NOT cerebras-fast. Cerebras is
# not free: its observed blended rate is ~$1.90/1M, which made it 8x pricier
# per briefing and the single largest line item across the whole LLM fleet.
# Same reasoning already applied to the pill judge (see pipeline/pill_eval.py).
BRIEFING_MODEL = "accounts/fireworks/models/gpt-oss-120b"
BRIEFING_FRESH_S = 3600  # default cache age that triggers background regeneration


# How long a briefing stays "fresh" depends on the window it summarizes: a 3h
# briefing goes stale fast, a 30-day one barely moves between prewarm runs.
# A flat 1h meant that with prewarms every ~4h, three of every four hours a
# visit silently regenerated anyway — paying for prewarm AND on-demand.
# Keep the shortest window at or above the prewarm interval.
FRESH_BY_HOURS = {
    3: 3 * 3600,        # matches the tightest prewarm cadence
    6: 4 * 3600,
    24: 6 * 3600,
    72: 12 * 3600,
    168: 24 * 3600,
    720: 24 * 3600,
}


def fresh_s(hours) -> int:
    """Seconds a cached briefing for this window counts as fresh."""
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return BRIEFING_FRESH_S
    if h in FRESH_BY_HOURS:
        return FRESH_BY_HOURS[h]
    # Unlisted window: scale with it, clamped to the range above.
    return max(3 * 3600, min(24 * 3600, h * 900))
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


def _trace_generation(name, prompt, output, usage, metadata=None):
    """Record one non-streaming generation in Langfuse. Never raises.

    Worth having for every call, not just the briefing: the editor's real cost
    is dominated by reasoning tokens, which are invisible in the response body
    but billed as output, so per-call usage is the only honest cost signal.
    """
    lf = _get_langfuse()
    if not lf:
        return
    try:
        with lf.start_as_current_observation(
            name=name, as_type="generation", model=BRIEFING_MODEL,
            input=prompt[:2000], output=(output or "")[:4000],
            metadata=metadata or {},
        ) as gen:
            if usage:
                gen.update(usage_details={
                    "input": usage.get("prompt_tokens"),
                    "output": usage.get("completion_tokens"),
                    "total": usage.get("total_tokens"),
                })
    except Exception:
        req_log.debug("Langfuse trace failed for %s", name, exc_info=True)


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


# Normalize citation formatting variance across model outputs: the same model
# writes [3] on one run and 【3】 the next. These patterns convert exotic
# brackets back to plain [N] so SSR (_briefing_html in pages.py) and the
# streaming client (markdown.js) produce identical HTML.
_CITE_BRACKETS = re.compile(r"[【〔［\[]{1,2}\s*(\d+(?:\s*[,，、]\s*\d+)*)\s*[】〕］\]]{1,2}")
_CITE_TRAILING_PUNCT = re.compile(r"([.!?])\s*((?:\[\d+\])+)\s*$", re.M)
_CITE_LEADING_SPACE = re.compile(r"[ \t]+((?:\[\d+\])+)")
_LEAD_LABEL = re.compile(r"^\*\*(?:Executive Summary|Summary|Overview|Lead|TL;DR)\s*:?\*\*\s*:?\s*", re.I)


def _normalize_text(text):
    """Fold model formatting variance back to plain ASCII markdown.

    The model cites [3] on one run and 【3】 the next, and sprinkles U+202F
    narrow no-break spaces / U+2011 non-breaking hyphens that fall back to a
    different font and open visible gaps mid-word."""
    text = _CITE_BRACKETS.sub(
        lambda m: "".join(f"[{n.strip()}]" for n in re.split(r"[,，、]", m.group(1))),
        text,
    )
    text = text.translate({0x00A0: " ", 0x2009: " ", 0x202F: " ", 0x2011: "-"})
    # Citation placement drifts too ("week[3]." vs "week. [3]"). Pull a
    # line-final period back inside and drop any space before a marker.
    text = _CITE_TRAILING_PUNCT.sub(r"\2\1", text)
    return _CITE_LEADING_SPACE.sub(r"\1", text)


def _normalize_briefing(text, view_name, hours):
    """Post-process a model-generated briefing for consistent structure.

    Small models produce variable formatting — stray bullets, duplicate
    text, missing headers. This pass enforces a clean, predictable shape
    before the briefing is cached, so the dashboard never shows mess."""
    text = _normalize_text(text.strip())

    # Build the canonical header
    if hours <= 1:
        time_label = "the last hour"
    elif hours <= 24:
        time_label = f"Last {hours} Hours"
    elif hours <= 72:
        time_label = f"Last {hours // 24} Days"
    elif hours <= 168:
        time_label = "Last Week"
    elif hours <= 720:
        time_label = "Last 30 Days"
    else:
        time_label = f"Last {hours // 24} Days"
    canonical_header = f"## {view_name} — {time_label}"

    # 1. Ensure the canonical header is present.
    #    If the model wrote a different H2 header, replace it.
    #    If there's no H2 header at all, prepend one.
    lines = text.split("\n")
    header_replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            lines[i] = canonical_header
            header_replaced = True
            break
    if not header_replaced:
        lines.insert(0, canonical_header)
        lines.insert(1, "")  # blank line after header

    # 2. Fix stray bullet characters. Normalize all to "- ".
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- - "):
            stripped = "- " + stripped[4:]
        elif stripped.startswith("* - "):
            stripped = "- " + stripped[4:]
        elif stripped.startswith("*  ") and not stripped.startswith("* **"):
            stripped = "- " + stripped[3:]
        elif stripped.startswith("* ") and len(stripped) > 2:
            if not stripped[2:].startswith("**"):
                stripped = "- " + stripped[2:]
        cleaned.append(stripped)

    # 3. Collapse repeated blank lines (max 1 blank between sections).
    final = []
    prev_blank = False
    for line in cleaned:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        prev_blank = is_blank
        final.append(line)

    return "\n".join(final).strip()


def _json_array(text: str):
    """Parse a JSON array out of a model response, tolerating a stray code
    fence and surrounding prose. Raises ValueError if there is no array."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array in response")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, list):
        raise ValueError("response is not a list")
    return parsed


# gpt-oss-120b is a reasoning model: it spends output budget on hidden
# reasoning before emitting anything, and those tokens are billed as output.
# At max_tokens=1800 a 40-candidate selection came back COMPLETELY EMPTY —
# not truncated, empty — because the budget was gone before the JSON started.
# Give any structured call real headroom; the same note explains the writer's
# 8000 further down.
EDITOR_MAX_TOKENS = 4000


def _chat(prompt: str, max_tokens: int, temperature: float,
          timeout: int = 90, reasoning_effort: str | None = None) -> tuple[str, dict]:
    """One non-streaming gateway completion -> (content, usage)."""
    from urllib.request import Request, urlopen

    key = _get_openrouter_key()
    if not key:
        raise RuntimeError("no gateway key")
    body_kwargs = {
        "model": BRIEFING_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if reasoning_effort:
        # Reasoning tokens are billed as output and dominate the editor's cost.
        # Selection is a judgement call over a short list, not a derivation, so
        # it does not need deep reasoning. Ignored harmlessly by models and
        # gateways that do not support the field.
        body_kwargs["reasoning_effort"] = reasoning_effort
    payload = json.dumps(body_kwargs).encode()
    req = Request(OPENROUTER_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    with urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    return content, (body.get("usage") or {})


def _fmt_source(s, prev_links=None):
    """One candidate rendered for a prompt: [outlet × N sources] Title — desc."""
    outlet = s.get("outlet") or "source"
    n = s.get("n_sources") or 1
    tag = f"{outlet} × {n} sources" if n > 1 else outlet
    if prev_links and s.get("link") not in prev_links:
        tag = f"NEW × {tag}"
    line = f"[{tag}] {s.get('title') or ''}"
    desc = (s.get("description") or "").strip()
    if desc:
        line += f" — {desc}"
    return line


def _time_label(hours):
    if hours <= 1:
        return "the last hour"
    if hours <= 24:
        return f"the last {hours} hours"
    if hours <= 72:
        return f"the last {hours // 24} days"
    if hours <= 168:
        return "the last week"
    return f"the last {hours // 24} days"


def _select_events(candidates: list[dict], view_name: str, view_desc: str,
                   hours: int, threads: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Editor pass: choose which candidates the briefing should cover.

    Returns ``(selected, verdicts)``. ``selected`` is a list of candidate dicts,
    each carrying its 1-based candidate number as ``n`` and the editor's
    justification as ``editor_reason``. ``verdicts`` is the full per-candidate
    record kept for the ⓘ panel, including what was passed over and why.

    Separating selection from writing does two things a single call could not.
    The writer stops having to be an editor while composing, and the selection
    becomes inspectable — the reader can see forty candidates narrowed to ten,
    with reasons.

    FAILS OPEN. Any gateway or parse failure returns the importance-ranked top
    BRIEFING_SELECT_TARGET with no reasons attached, which is exactly the old
    behaviour. A briefing must never fail because the editor did.
    """
    fallback = candidates[:BRIEFING_SELECT_TARGET]
    for i, s in enumerate(fallback):
        s.setdefault("n", i + 1)
    if len(candidates) <= BRIEFING_SELECT_TARGET:
        return fallback, []

    for i, s in enumerate(candidates):
        s["n"] = i + 1
    listing = "\n".join(f"{s['n']}. {_fmt_source(s)}" for s in candidates)

    thread_block = ""
    active = [t for t in (threads or []) if (t.get("status") or "active") == "active"]
    if active:
        thread_block = (
            "STORYLINES ALREADY BEING TRACKED for this feed — a candidate that advances "
            "one of these is more valuable than an isolated item of similar size:\n"
            + "\n".join(f"- {t.get('title')}: {t.get('summary')}" for t in active[:8])
            + "\n\n"
        )

    topic = (f'The feed is "{view_name}" — {view_desc}. ' if view_name and view_name != "Global News"
             else "The feed is general world news. ")

    prompt = (
        f"You are the editor of a news briefing, deciding what it should cover. "
        f"{topic}The window is {_time_label(hours)}.\n\n"
        f"{thread_block}"
        f"Below are {len(candidates)} candidate stories, de-duplicated into distinct events. "
        f"'× N sources' means N outlets carried it, which is a signal of reach but NOT of "
        f"importance — a wire story reprinted 30 times is still one story, and a single-source "
        f"report can be the most consequential item here.\n\n"
        f"Choose about {BRIEFING_SELECT_TARGET} (between 8 and 12) for the briefing. Judge on:\n"
        f"- consequence: who is affected, what changes, what is at stake\n"
        f"- newness: something happened, rather than a standing situation being described\n"
        f"- advancing a tracked storyline listed above\n\n"
        f"Deliberately include AT LEAST ONE consequential but under-covered story — a "
        f"single-source or low-coverage item a reader would not have seen elsewhere. Do not "
        f"pad this with a second angle on the lead story.\n\n"
        f"Reject as 'not_news' anything that is not a news article about an event: section "
        f"index and navigation pages, job postings, listicles, evergreen explainers and buying "
        f"guides, quote/profile/listing pages, and bot or paywall interstitials.\n\n"
        f"Output ONLY a JSON array (no prose, no code fence). Include an object ONLY for:\n"
        f"  - each story you choose, and\n"
        f"  - each story you are rejecting as not a news article.\n"
        f"Say NOTHING about the rest — do not emit an object for a candidate you are simply "
        f"not picking. Keep it to roughly {BRIEFING_SELECT_TARGET + 6} objects.\n"
        f'{{"n": <candidate number>, "verdict": "chosen"|"not_news", "reason": "<8 words max>"}}\n\n'
        f"Candidates:\n{listing}"
    )

    t0 = time.monotonic()
    try:
        raw, usage = _chat(prompt, max_tokens=EDITOR_MAX_TOKENS, temperature=0.2,
                           reasoning_effort="low")
        verdicts = _json_array(raw)
    except Exception as e:
        req_log.warning("briefing editor failed (%s) — falling back to importance order", e)
        return fallback, []
    _trace_generation("gdelt-briefing-editor", prompt, raw, usage,
                      {"view": view_name, "hours": hours, "n_candidates": len(candidates),
                       "latency_s": round(time.monotonic() - t0, 2)})

    by_n = {s["n"]: s for s in candidates}
    clean, chosen = [], []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        try:
            n = int(v.get("n"))
        except (TypeError, ValueError):
            continue
        if n not in by_n:
            continue
        verdict = v.get("verdict")
        if verdict not in ("chosen", "not_news", "passed"):
            continue
        reason = (v.get("reason") or "").strip()[:120]
        clean.append({"n": n, "verdict": verdict, "reason": reason,
                      "title": (by_n[n].get("title") or "")[:110]})
        if verdict == "chosen":
            src = by_n[n]
            src["editor_reason"] = reason
            chosen.append(src)

    if not chosen:
        req_log.warning("briefing editor selected nothing — falling back to importance order")
        return fallback, clean

    # Guard against a runaway selection; keep the editor's own ordering.
    chosen = chosen[:BRIEFING_SELECT_TARGET + 2]
    req_log.info("briefing editor: %d candidates -> %d chosen in %.1fs",
                 len(candidates), len(chosen), time.monotonic() - t0)
    return chosen, clean


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

    # Numbering is the CANDIDATE index (1..N over the full candidate list), not
    # a fresh 1..len(selected). sources_json persists all candidates in that
    # order and the client maps [N] -> sources[N-1], so renumbering here would
    # silently point every citation at the wrong story.
    numbered = "\n".join(
        f"{s.get('n', i + 1)}. {_fmt_source(s, prev_links)}"
        + (f"\n   (selected: {s['editor_reason']})" if s.get("editor_reason") else "")
        for i, s in enumerate(sources)
    )

    time_label = _time_label(hours)

    topic_context = ""
    if view_name and view_name != "Global News":
        topic_context = (
            f'This feed monitors "{view_name}" — {view_desc}. '
            f"Focus your analysis on developments relevant to this topic. "
        )

    format_instructions = (
        f"OutPUT THIS EXACT STRUCTURE, nothing else:\n\n"
        f"## {view_name} — {time_label}\n\n"
        f"A 2-3 sentence lede about the biggest stories.\n\n"
        f"- **Topic:** one-sentence description [1]\n"
        f"- **Topic:** one-sentence description [3]\n\n"
        f"(5-8 bullets in that exact format. Bracketed numbers like [1] are "
        f"citations — use real numbers from the list below, never write [N].)\n\n"
        f"What to watch next: one sentence.\n\n"
        f"Do not write the words CITATIONS, IMPORTANT, or NOTE anywhere. "
        f"Do not repeat or list the sources. Cover stories 1-5. Be specific.\n\n"
    ) if "local" in BRIEFING_MODEL else (
        f"Write a structured intelligence briefing in this format:\n\n"
        f"Start with a markdown H2 header line (begins with '## ') stating the topic and time window "
        f"(e.g., '## AI & Machine Learning — Last 24 Hours' or '## Global News — Last 7 Days'). "
        f"The topic is \"{view_name}\" and the time window is {time_label}.\n\n"
        f"Then write a 3-5 sentence executive summary that LEADS with the single most "
        f"consequential new development — a specific event, actor, and stake — not a survey. "
        f"Say why it matters and, where the context below supports it, how it relates to what "
        f"you reported previously. "
        f"Never open with panoramic filler like 'The global landscape is dominated by…' or "
        f"'The last {hours} hours have been marked by…'.\n\n"
        f"Then provide 8-12 key highlights as a markdown bullet list (use '- ' for each bullet). "
        f"Open each with a short bolded label, then TWO sentences: the first states what "
        f"happened, naming concrete companies, countries, people or figures; the second says why "
        f"it matters — the consequence, who is exposed, what it connects to, or what it changes. "
        f"Do not pad the second sentence with restatement; if a story genuinely warrants only one "
        f"sentence, leave it at one.\n\n"
        f"Then a section '**What to watch:**' — a short paragraph (2-4 sentences), not a single "
        f"line, on what would confirm or break the developments above.\n\n"
        f"Then a section '**Quieter but notable:**' — one or two sentences on a single "
        f"less-covered story from the list that could matter later. Pick something genuinely "
        f"under-reported rather than a second take on the lead story. Omit this section only if "
        f"nothing in the list qualifies.\n\n"
        f"CITATIONS: After each highlight (and any specific factual claim), cite the supporting "
        f"story/stories using ASCII square brackets that match the numbered list below, e.g. '[3]' or "
        f"'[3][7]'. Each number is one story even if covered by many outlets — cite it once, not per "
        f"outlet. Cite only stories that directly support the claim; aim for 1-2 citations per "
        f"highlight. Use only numbers from the list — never invent numbers, and never write URLs.\n\n"
        f"NUMBERING: the numbers in the list below are fixed identifiers, not positions. Use each "
        f"story's own number exactly as given. Do NOT renumber them 1, 2, 3 in the order you "
        f"write about them.\n\n"
        f"COVERAGE DUTY: every story in the list below was chosen deliberately for this briefing, "
        f"and most carry a note explaining why. Cover all of them, even where one does not fit "
        f"the prior narrative.\n\n"
        f"Be specific and concrete. No filler. No hedging. "
        f"Use markdown formatting for emphasis and structure.\n\n"
    )

    selected = any(s.get("editor_reason") for s in sources)
    provenance = (
        "These were SELECTED FOR YOU by an editor from a larger candidate pool, on "
        "consequence rather than volume; most carry a one-line note on why it was picked. "
        "Their numbers are identifiers from that pool, so they are not consecutive — "
        "that is expected.\n\n"
        if selected else
        "The list is RANKED BY IMPORTANCE (coverage + momentum + recency): low-numbered items "
        "are the biggest stories right now.\n\n"
    )
    return (
        f"You are a senior news intelligence analyst writing a briefing for a decision-maker. "
        f"Below are {len(sources)} numbered news stories from {time_label}, drawn from "
        f"44,000+ global sources monitored in real-time and de-duplicated into distinct events "
        f"(a story covered by many outlets shows a 'N sources' count and is a single numbered item).\n"
        f"{provenance}"
        f"{topic_context}"
        f"{prev_block}"
        f"{threads_block}"
        f"{format_instructions}"
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


def record_briefing_history(cache_key, view_id, hours, briefing, sources_json,
                            article_count, meta_json, generated_at, trigger="visit"):
    """Append one generation row to the permanent briefing_history table.

    Failure is logged only — never affects the briefing the user just
    received. Called from api_briefing after a successful cache write (both
    SSE and non-streaming paths). ``trigger`` distinguishes organic visits
    ('visit') from future pre-warm/manual-regenerate traffic so a bot reader
    can filter synthetic generations out if desired."""
    try:
        from models import get_user_db
        con = get_user_db()
        con.execute(
            "INSERT INTO briefing_history "
            "(cache_key, view_id, hours, generated_at, briefing, "
            " article_count, sources_json, meta_json, trigger) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cache_key, view_id, hours, generated_at,
             briefing, article_count, sources_json, meta_json, trigger),
        )
        con.commit()
        con.close()
    except Exception:
        req_log.warning("briefing_history insert failed for %s", cache_key, exc_info=True)


BRIEFING_EVENT_BUFFER = 400   # articles pulled before dedup (pills/filtered)
# Candidates handed to the EDITOR, and how many it aims to keep. This was a
# single limit of 12 until 2026-08-26: the importance score picked 12 and the
# model narrated them in order, with no editorial judgement anywhere.
#
# 12 was itself a cost cut (50 -> 20 -> 12, commit 1aabb80, "Cut briefing LLM
# spend ~30x"), but measured against real stored prompts a source line is ~64
# tokens, so 12->40 costs about $0.24/month. The saving was never the point of
# that commit — leaving Cerebras and fixing prewarm were.
BRIEFING_CANDIDATE_LIMIT = 40
BRIEFING_SELECT_TARGET = 10
# Kept: imported by routes/api_briefing.py and surfaced on /methodology via
# _doc_facts(). Now means "how many the editor aims to select".
BRIEFING_EVENT_LIMIT = BRIEFING_SELECT_TARGET
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

        # Clustering leaves near-identical headlines in the ranked list — measured
        # at ~3 repeats per prompt, which burned tokens and pushed the model to
        # write the same bullet twice. Dedup on a normalized title BEFORE taking
        # the top N, so the model gets N *distinct* stories rather than N rows.
        seen = set()
        for c in _window_events(con, view, cutoff):
            if len(events) >= BRIEFING_CANDIDATE_LIMIT:
                break
            title = (c.get("title") or "").strip()
            if not usable_title(title):
                continue
            key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:70]
            if key in seen:
                continue
            seen.add(key)
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


def thread_key(cache_key: str) -> str:
    """view_id from a 'view:hours' briefing cache key.

    Threads belong to a topic, not a time window: a story is 'day 4 of' the
    same story whether you are reading the 3h or the 30d briefing."""
    return cache_key.split(":", 1)[0] if ":" in cache_key else cache_key


def get_threads(cache_key: str) -> list[dict]:
    """Load active story threads for a view. Never raises."""
    try:
        from models import get_user_db
        con = get_user_db()
        row = con.execute(
            "SELECT threads_json FROM briefing_threads WHERE view_id = ?",
            (thread_key(cache_key),),
        ).fetchone()
        con.close()
        if row and row["threads_json"]:
            threads = json.loads(row["threads_json"])
            return threads if isinstance(threads, list) else []
    except Exception:
        req_log.debug("thread load failed for %s", cache_key, exc_info=True)
    return []


# A thread list is per-VIEW, so the six windows of one view would otherwise each
# pay for the same update. Rate-limit per view, and on prewarm only bother for
# views a human has actually opened recently — the same demand signal
# prewarm_briefings.py uses to decide what to warm at all.
THREAD_MIN_INTERVAL_S = 3 * 3600
THREAD_DEMAND_DAYS = 30


def should_update_threads(cache_key: str, trigger: str) -> bool:
    """Whether this generation should pay for a thread update. Never raises."""
    view_id = thread_key(cache_key)
    try:
        from models import get_user_db
        con = get_user_db()
        try:
            row = con.execute(
                "SELECT (julianday('now') - julianday(updated_at)) * 86400.0 AS age "
                "FROM briefing_threads WHERE view_id = ?", (view_id,)
            ).fetchone()
            if row and row["age"] is not None and row["age"] < THREAD_MIN_INTERVAL_S:
                return False
            if trigger != "prewarm":
                return True   # a human is reading this one; always keep it current
            seen = con.execute(
                "SELECT 1 FROM briefing_history WHERE trigger = 'visit' AND view_id = ? "
                "AND generated_at >= date('now', ?) LIMIT 1",
                (view_id, f"-{THREAD_DEMAND_DAYS} day"),
            ).fetchone()
            return bool(seen)
        finally:
            con.close()
    except Exception:
        req_log.debug("thread gate check failed for %s", view_id, exc_info=True)
        return trigger != "prewarm"


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
        f"- ADD a new thread (first_seen = today) only for a story that is genuinely likely to "
        f"produce follow-up news AND is substantial enough to be worth tracking: conflicts, "
        f"disasters, investigations, elections, policy fights, market-moving events. Prefer "
        f"stories carried by several outlets. A one-off human-interest item, a celebrity rumour "
        f"or a lottery result is NOT a thread. Adding nothing is a perfectly good outcome.\n"
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
        "INSERT OR REPLACE INTO briefing_threads (view_id, threads_json, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        (thread_key(cache_key), json.dumps(threads)),
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
