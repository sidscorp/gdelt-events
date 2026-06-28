"""AI-briefing generation for the GDELT dashboard.

Self-contained briefing logic extracted from app.py (pure move). The Flask
route ``api_briefing`` stays in app.py and imports the needed symbols from
this module.
"""

import json
import time
import logging

from db import get_db, _hours_cutoff
from views import find_view
from _paths import OPENROUTER_KEY_PATH

# Same singleton logger app.py configures; getLogger by name returns it (no
# circular import on app).
req_log = logging.getLogger("dashboard.requests")


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
BRIEFING_MODEL = "google/gemini-2.5-flash"
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


def _build_briefing_prompt(sources: list[dict], view_name: str, view_desc: str,
                            hours: int) -> str:
    """Build a time-aware, context-rich briefing prompt with numbered sources
    the model can cite by index."""
    def _fmt(s):
        outlet = s.get("outlet") or "source"
        n = s.get("n_sources") or 1
        tag = f"{outlet} · {n} sources" if n > 1 else outlet
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
        f"(a story covered by many outlets shows a 'N sources' count and is a single numbered item).\n\n"
        f"{topic_context}"
        f"Write a structured intelligence briefing in this format:\n\n"
        f"Start with a markdown H2 header line (begins with '## ') stating the topic and time window "
        f"(e.g., '## AI & Machine Learning — Last 24 Hours' or '## Global News — Last 7 Days'). "
        f"The topic is \"{view_name}\" and the time window is {time_label}.\n\n"
        f"Then write a 3-4 sentence executive summary of the overall landscape.\n\n"
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
        f"Be specific and concrete. No filler. No hedging. "
        f"Use markdown formatting for emphasis and structure.\n\n"
        f"Numbered sources:\n{numbered}"
    )


def _generate_briefing_stream(sources, view_name, view_desc, hours):
    """Stream briefing tokens from OpenRouter as SSE.

    When Langfuse is configured, the full generation (model, token usage, cost,
    latency, view/hours) is logged once the stream completes."""
    import time
    from urllib.request import Request, urlopen

    key = _get_openrouter_key()
    if not key:
        return

    prompt = _build_briefing_prompt(sources, view_name, view_desc, hours)
    payload = json.dumps({
        "model": BRIEFING_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0.3,
        "stream": True,
        "usage": {"include": True},  # OpenRouter: emit token usage + cost in the final chunk
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
        with urlopen(req, timeout=60) as resp:
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


def _generate_briefing(sources, view_name, view_desc, hours):
    """Non-streaming briefing generation (for caching)."""
    parts = []
    try:
        for chunk in _generate_briefing_stream(sources, view_name, view_desc, hours):
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


def _dedup_rank_events(con, rows):
    """rows=[(url,title,outlet,desc)] -> distinct events (one per cluster, newest
    member kept), ranked by source coverage then recency, capped."""
    urls = [r[0] for r in rows if r[0]]
    cmap = _cluster_ids(con, urls)
    sizes = _cluster_sizes(con, set(cmap.values()))
    seen, events = set(), []
    for url, title, outlet, desc in rows:
        if not title or len(title.strip()) <= 10:
            continue
        cid = cmap.get(url)
        key = cid or url
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "title": title, "description": (desc or "")[:BRIEFING_DESC_CHARS],
            "outlet": outlet, "n_sources": (sizes.get(cid, 1) if cid else 1),
            "link": f"/event/{cid}" if cid else url,
        })
    events.sort(key=lambda e: e["n_sources"], reverse=True)  # stable: recency tiebreak
    return events[:BRIEFING_EVENT_LIMIT]


def _global_briefing_events(con, cutoff):
    """Global feed: the window's most-covered events (from the clusters table,
    so it spans the whole window, not just the newest minutes) plus the latest
    breaking single-source items."""
    events, seen_cids = [], set()
    try:
        for cid, title, size, mjson in con.execute(
            "SELECT cluster_id, title, size, members_json FROM clusters "
            "WHERE latest_seen >= ? AND status = 'active' ORDER BY size DESC LIMIT 35",
            [cutoff],
        ).fetchall():
            outlet, desc = _rep_from_members(mjson)
            events.append({"title": title, "description": desc, "outlet": outlet,
                           "n_sources": size, "link": f"/event/{cid}"})
            seen_cids.add(cid)
    except Exception:
        pass
    # recent breaking singletons (newest articles not in any cluster)
    try:
        rows = con.execute(
            "SELECT url, title, outlet_name, description FROM gal "
            "WHERE crawled_at >= ? AND language = 'en' AND title IS NOT NULL "
            "ORDER BY crawled_at DESC LIMIT 80",
            [cutoff],
        ).fetchall()
        cmap = _cluster_ids(con, [r[0] for r in rows])
        added = 0
        for url, title, outlet, desc in rows:
            if added >= 15:
                break
            if cmap.get(url) or not title or len(title.strip()) <= 10:
                continue  # clustered -> covered above (or a minor cluster we skip)
            events.append({"title": title, "description": (desc or "")[:BRIEFING_DESC_CHARS],
                           "outlet": outlet, "n_sources": 1, "link": url})
            added += 1
    except Exception:
        pass
    return events


def _fetch_briefing_events(view_id, hours):
    """Fetch deduped EVENTS for a briefing (one per story, ranked by coverage),
    each with a 'link' to its event page (all outlets) or the article, and an
    'n_sources' count. Returns (events, view_name, view_desc)."""
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

        if view is None:
            events = _global_briefing_events(con, cutoff)
        elif view.get("kind") == "tag_match":
            rows = con.execute(
                "SELECT g.url, g.title, g.outlet_name, g.description FROM article_tags t "
                "JOIN gal g ON g.url = t.article_id "
                "WHERE t.category = ? AND t.source_type = 'gal' AND t.crawled_at >= ? "
                "ORDER BY t.crawled_at DESC LIMIT ?",
                [view["tag_category"], cutoff, BRIEFING_EVENT_BUFFER],
            ).fetchall()
            events = _dedup_rank_events(con, rows)
        elif view.get("kind") == "fda_match":
            rows = con.execute(
                "SELECT g.url, g.title, g.outlet_name, g.description FROM fda_match_cache f "
                "JOIN gal g ON g.url = f.article_id "
                "WHERE f.source_type = 'gal' AND f.crawled_at >= ? "
                "ORDER BY f.crawled_at DESC LIMIT ?",
                [cutoff, BRIEFING_EVENT_BUFFER],
            ).fetchall()
            events = _dedup_rank_events(con, rows)
        else:
            rows = con.execute(
                "SELECT url, title, outlet_name, description FROM gal "
                "WHERE crawled_at >= ? AND language = 'en' AND title IS NOT NULL "
                "ORDER BY crawled_at DESC LIMIT ?",
                [cutoff, BRIEFING_EVENT_BUFFER],
            ).fetchall()
            events = _dedup_rank_events(con, rows)
    except Exception:
        events = []
    finally:
        try:
            con.close()
        except Exception:
            pass

    return events, view_name, view_desc
