"""AI briefing + FDA events API routes."""

import json
import threading
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

import time as _time

from db import get_db
from briefing import (
    BRIEFING_FRESH_S, BRIEFING_EVENT_LIMIT, BRIEFING_MODEL, fresh_s,
    _build_briefing_prompt, _fetch_briefing_events,
    _generate_briefing, _generate_briefing_stream,
    _normalize_briefing,
    get_threads, record_briefing_history, update_threads_async,
)

bp = Blueprint("api_briefing", __name__)

# Concurrent regeneration guard: at most one fresh generation per cache_key
# at a time. Subsequent requests for the same stale key serve cached content
# only (no duplicate LLM calls).
_regen_lock = threading.Lock()
_regen_in_flight: set = set()


def _try_start_regen(cache_key: str) -> bool:
    """Acquire regeneration slot. Returns True if this caller should generate."""
    with _regen_lock:
        if cache_key in _regen_in_flight:
            return False
        _regen_in_flight.add(cache_key)
        return True


def _finish_regen(cache_key: str):
    """Release regeneration slot."""
    with _regen_lock:
        _regen_in_flight.discard(cache_key)


@bp.route("/api/briefing")
def api_briefing():
    """AI-generated briefing with SSE streaming.

    Serve cached content immediately (even if stale), then if the cache is
    older than BRIEFING_FRESH_S, generate fresh content on the same SSE
    connection so the user sees old text instantly with a seamless update."""
    from flask import Response

    view_id = (request.args.get("view") or "").strip()
    hours = request.args.get("hours", 24, type=int)
    stream = request.args.get("stream", "0") == "1"
    # Two non-visitor modes, deliberately different:
    #   refresh=1  force regeneration, ignoring freshness (manual / debugging)
    #   prewarm=1  scheduled warm — regenerate ONLY if stale for this window
    # The scheduled job uses prewarm=1 so fresh_s() governs it. With refresh=1 it
    # rewrote every combo on every run, which for a 30-day briefing (fresh 24h)
    # meant five regenerations a day of content that had barely moved.
    refresh = request.args.get("refresh") == "1"
    prewarm = request.args.get("prewarm") == "1"

    cache_key = f"{view_id or '_all'}:{hours}"
    # Both modes tag history as 'prewarm', which also suppresses the thread
    # update and keeps prewarm's own writes out of the demand signal.
    history_trigger = "prewarm" if (refresh or prewarm) else "visit"

    from models import get_user_db

    # ── phase 0: read cache (used by both streaming and non-streaming paths) ──
    ucon = get_user_db()
    cached = ucon.execute(
        "SELECT briefing, article_count, generated_at, sources_json FROM briefing_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    ucon.close()

    is_stale = False
    cached_sources = []
    if cached and not refresh:
        try:
            cached_sources = json.loads(cached["sources_json"]) if cached["sources_json"] else []
        except Exception:
            cached_sources = []
        age_s = (datetime.utcnow() - datetime.strptime(cached["generated_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        # Freshness scales with the window being summarized — see briefing.fresh_s.
        is_stale = age_s >= fresh_s(hours)
    elif not cached:
        is_stale = True  # no cache at all — must generate

    needs_regen = is_stale or refresh
    should_regen = needs_regen and _try_start_regen(cache_key)

    # ── non-streaming path ──
    if not stream:
        if cached and not needs_regen:
            return jsonify({
                "briefing": cached["briefing"],
                "sources": cached_sources,
                "article_count": cached["article_count"],
                "generated_at": cached["generated_at"],
                "cached": True,
                "view": view_id or None,
                "hours": hours,
            })
        if not should_regen:
            # Another request is already regenerating; just return stale content
            if cached:
                return jsonify({
                    "briefing": cached["briefing"],
                    "sources": cached_sources,
                    "article_count": cached["article_count"],
                    "generated_at": cached["generated_at"],
                    "cached": True,
                    "stale": True,
                    "view": view_id or None,
                    "hours": hours,
                })
            return jsonify({"briefing": None, "error": "Generation in progress, no cache yet"}), 503

        prev = None
        if cached:
            try:
                prev = {"briefing": cached["briefing"], "generated_at": cached["generated_at"],
                        "sources": cached_sources}
            except Exception:
                prev = None
        threads = get_threads(cache_key)
        sources, view_name, view_desc = _fetch_briefing_events(view_id, hours)
        try:
            if len(sources) < 2:
                if cached:
                    return jsonify({
                        "briefing": cached["briefing"],
                        "sources": cached_sources,
                        "article_count": cached["article_count"],
                        "generated_at": cached["generated_at"],
                        "cached": True,
                        "stale": True,
                        "view": view_id or None,
                        "hours": hours,
                    })
                return jsonify({"briefing": None, "error": "Not enough articles", "found": len(sources)})

            sources = sources[:BRIEFING_EVENT_LIMIT]
            sources_payload = [{"n": i + 1, "link": s["link"], "outlet": s.get("outlet"),
                                "title": s.get("title"), "n_sources": s.get("n_sources", 1)}
                               for i, s in enumerate(sources)]
            sources_json = json.dumps(sources_payload)
            prompt = _build_briefing_prompt(sources, view_name, view_desc, hours,
                                            prev=prev, threads=threads)
            gen_t0 = _time.time()

            briefing = _normalize_briefing(_generate_briefing(sources, view_name, view_desc, hours,
                                                               prev=prev, threads=threads, prompt=prompt), view_name, hours)
            if not briefing:
                if cached:
                    return jsonify({
                        "briefing": cached["briefing"], "sources": cached_sources,
                        "article_count": cached["article_count"],
                        "generated_at": cached["generated_at"], "cached": True, "stale": True,
                        "view": view_id or None, "hours": hours,
                    })
                return jsonify({"briefing": None, "error": "Briefing generation unavailable"}), 503

            generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            meta_json = json.dumps({
                "model": BRIEFING_MODEL, "prompt": prompt, "n_sources": len(sources),
                "elapsed_s": round(_time.time() - gen_t0, 2),
                "briefing_chars": len(briefing), "cache_ttl_s": BRIEFING_FRESH_S,
            })
            uc = get_user_db()
            uc.execute(
                "INSERT OR REPLACE INTO briefing_cache "
                "(cache_key, briefing, article_count, generated_at, sources_json, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cache_key, briefing, len(sources), generated_at, sources_json, meta_json),
            )
            uc.commit()
            uc.close()
            record_briefing_history(cache_key, view_id or "_all", hours, briefing,
                                    sources_json, len(sources), meta_json, generated_at,
                                    trigger=history_trigger)
            if history_trigger != "prewarm":
                update_threads_async(cache_key, threads, briefing)
            return jsonify({
                "briefing": briefing, "sources": sources_payload,
                "article_count": len(sources), "generated_at": generated_at,
                "cached": False, "view": view_id or None, "hours": hours,
            })
        finally:
            _finish_regen(cache_key)

    # ── SSE streaming path ──
    def sse_stream():
        # Phase 1: serve cached content IMMEDIATELY (even stale)
        if cached and not refresh:
            yield f"data: {json.dumps({'sources': cached_sources})}\n\n"
            yield f"data: {json.dumps({'text': cached['briefing'], 'done': False, 'cached': True, 'stale': is_stale, 'article_count': cached['article_count']})}\n\n"
            if not needs_regen:
                yield f"data: {json.dumps({'done': True, 'cached': True, 'article_count': cached['article_count']})}\n\n"
                return

        # Phase 2: if stale or no cache, attempt regeneration
        if not should_regen:
            # Another request is regenerating — serve stale and be done
            if cached:
                meta_label = "Updating — another request is refreshing this briefing"
                yield f"data: {json.dumps({'done': True, 'cached': True, 'stale': True, 'meta': meta_label, 'article_count': cached['article_count']})}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'Generation in progress', 'done': True})}\n\n"
            return

        try:
            prev = None
            if cached:
                try:
                    prev = {"briefing": cached["briefing"], "generated_at": cached["generated_at"],
                            "sources": cached_sources}
                except Exception:
                    prev = None
            threads = get_threads(cache_key)
            sources, view_name, view_desc = _fetch_briefing_events(view_id, hours)

            if len(sources) < 2:
                if cached:
                    yield f"data: {json.dumps({'done': True, 'cached': True, 'stale': True, 'article_count': cached['article_count']})}\n\n"
                else:
                    yield f"data: {json.dumps({'error': 'Not enough articles', 'found': len(sources), 'done': True})}\n\n"
                return

            sources = sources[:BRIEFING_EVENT_LIMIT]
            sources_payload = [{"n": i + 1, "link": s["link"], "outlet": s.get("outlet"),
                                "title": s.get("title"), "n_sources": s.get("n_sources", 1)}
                               for i, s in enumerate(sources)]
            sources_json = json.dumps(sources_payload)
            prompt = _build_briefing_prompt(sources, view_name, view_desc, hours,
                                            prev=prev, threads=threads)
            gen_t0 = _time.time()

            # Send fresh sources map (may differ from cached)
            yield f"data: {json.dumps({'sources': sources_payload})}\n\n"

            full_text = []
            try:
                for chunk in _generate_briefing_stream(sources, view_name, view_desc, hours,
                                                        prev=prev, threads=threads, prompt=prompt):
                    full_text.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
                return

            briefing = _normalize_briefing("".join(full_text).strip(), view_name, hours)
            if briefing:
                generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                meta_json = json.dumps({
                    "model": BRIEFING_MODEL, "prompt": prompt, "n_sources": len(sources),
                    "continuity_titles": [
                        (s.get("title") or "")[:110] for s in (prev or {}).get("sources", [])[:8]
                    ] if prev else [],
                    "threads_used": [
                        {"title": t.get("title"), "first_seen": t.get("first_seen"),
                         "summary": t.get("summary")}
                        for t in (threads or []) if (t.get("status") or "active") == "active"
                    ],
                    "elapsed_s": round(_time.time() - gen_t0, 2),
                    "briefing_chars": len(briefing), "cache_ttl_s": BRIEFING_FRESH_S,
                })
                try:
                    uc = get_user_db()
                    uc.execute(
                        "INSERT OR REPLACE INTO briefing_cache "
                        "(cache_key, briefing, article_count, generated_at, sources_json, meta_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (cache_key, briefing, len(sources), generated_at, sources_json, meta_json),
                    )
                    uc.commit()
                    uc.close()
                except Exception:
                    pass
                record_briefing_history(
                    cache_key, view_id or "_all", hours, briefing,
                    sources_json, len(sources), meta_json, generated_at,
                    trigger=history_trigger,
                )
                # Thread continuity is only read by a human opening the panel, and
                # costs ~0.84x the briefing itself (measured). Prewarm skips it.
                if history_trigger != "prewarm":
                    update_threads_async(cache_key, threads, briefing)
            yield f"data: {json.dumps({'done': True, 'refreshed': is_stale, 'article_count': len(sources)})}\n\n"
        finally:
            _finish_regen(cache_key)

    return Response(sse_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})


@bp.route("/api/briefing_meta")
def api_briefing_meta():
    """Transparency: the exact inputs of the currently-cached briefing —
    model, verbatim prompt, ranked sources, continuity context. Public."""
    view_id = (request.args.get("view") or "").strip()
    hours = request.args.get("hours", 24, type=int)
    cache_key = f"{view_id or '_all'}:{hours}"

    from models import get_user_db
    ucon = get_user_db()
    row = ucon.execute(
        "SELECT generated_at, article_count, sources_json, meta_json "
        "FROM briefing_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    ucon.close()
    if not row:
        return jsonify({"error": "No cached briefing for this view/window yet"}), 404

    meta = {}
    try:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
    except Exception:
        pass
    sources = []
    try:
        sources = json.loads(row["sources_json"]) if row["sources_json"] else []
    except Exception:
        pass
    age_s = None
    try:
        age_s = int((datetime.utcnow() - datetime.strptime(
            row["generated_at"], "%Y-%m-%d %H:%M:%S")).total_seconds())
    except Exception:
        pass
    return jsonify({
        "view": view_id or None,
        "hours": hours,
        "generated_at": row["generated_at"],
        "age_s": age_s,
        "article_count": row["article_count"],
        "sources": sources,
        "meta": meta,
        "methodology": "/methodology",
    })


@bp.route("/api/fda_events")
def api_fda_events():
    """Recent FDA device regulatory events (recalls, 510k clearances, enforcement).

    Query params:
        days: how many days back to return (default 30, max 90)
        type: filter by event_type ('recall', '510k', 'enforcement')
        firm: case-insensitive substring filter on firm_name
    """
    try:
        days = max(1, min(90, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    event_type = (request.args.get("type") or "").strip().lower()
    firm = (request.args.get("firm") or "").strip()

    cutoff_date = int(
        (datetime.utcnow() - timedelta(days=days)).strftime("%Y%m%d")
    )

    con = get_db()
    if con is None:
        return jsonify({"error": "Database busy"}), 503
    try:
        conds = ["event_date >= ?"]
        params: list = [cutoff_date]
        if event_type in ("recall", "510k", "enforcement"):
            conds.append("event_type = ?")
            params.append(event_type)
        if firm:
            conds.append("firm_name ILIKE ?")
            params.append(f"%{firm}%")
        where = " AND ".join(conds)
        rows = con.execute(
            f"SELECT event_id, event_type, event_date, firm_name, "
            f"       product_description, recall_class, reason_for_recall, status "
            f"FROM fda_regulatory_events "
            f"WHERE {where} "
            f"ORDER BY event_date DESC "
            f"LIMIT 200",
            params,
        ).fetchall()
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500
    finally:
        try: con.close()
        except Exception: pass

    events = []
    for r in rows:
        eid, etype, edate, fname, pdesc, rclass, reason, status = r
        events.append({
            "event_id": eid,
            "event_type": etype,
            "event_date": str(edate) if edate else None,
            "firm_name": fname,
            "product_description": pdesc,
            "recall_class": rclass,
            "reason_for_recall": reason,
            "status": status,
        })

    return jsonify({"events": events, "days": days, "total": len(events)})
