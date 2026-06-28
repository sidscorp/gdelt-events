"""AI briefing + FDA events API routes."""

import json
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

from db import get_db
from briefing import (
    BRIEFING_TTL_S, BRIEFING_EVENT_LIMIT,
    _fetch_briefing_events, _generate_briefing, _generate_briefing_stream,
)

bp = Blueprint("api_briefing", __name__)


@bp.route("/api/briefing")
def api_briefing():
    """AI-generated briefing with optional SSE streaming."""
    from flask import Response

    view_id = (request.args.get("view") or "").strip()
    hours = request.args.get("hours", 24, type=int)
    stream = request.args.get("stream", "0") == "1"
    refresh = request.args.get("refresh") == "1"  # pre-warm: force regeneration

    cache_key = f"{view_id or '_all'}:{hours}"

    # Check cache first (both streaming and non-streaming)
    from models import get_user_db
    ucon = get_user_db()
    cached = ucon.execute(
        "SELECT briefing, article_count, generated_at, sources_json FROM briefing_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if cached and not refresh:
        age_s = (datetime.utcnow() - datetime.strptime(cached["generated_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        if age_s < BRIEFING_TTL_S:
            ucon.close()
            try:
                cached_sources = json.loads(cached["sources_json"]) if cached["sources_json"] else []
            except Exception:
                cached_sources = []
            if stream:
                # Send cached sources + briefing as SSE events
                def cached_stream():
                    yield f"data: {json.dumps({'sources': cached_sources})}\n\n"
                    yield f"data: {json.dumps({'text': cached['briefing'], 'done': True, 'cached': True, 'article_count': cached['article_count']})}\n\n"
                return Response(cached_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
            return jsonify({
                "briefing": cached["briefing"],
                "sources": cached_sources,
                "article_count": cached["article_count"],
                "generated_at": cached["generated_at"],
                "cached": True,
                "view": view_id or None,
                "hours": hours,
            })
    ucon.close()

    # Fetch deduped EVENTS for the selected window (no auto-widen — stays
    # consistent with the feed). If too sparse, "Not enough articles".
    sources, view_name, view_desc = _fetch_briefing_events(view_id, hours)
    if len(sources) < 2:
        _found = len(sources)
        if stream:
            def empty_stream(_f=_found):
                yield f"data: {json.dumps({'error': 'Not enough articles', 'found': _f, 'done': True})}\n\n"
            return Response(empty_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
        return jsonify({"briefing": None, "error": "Not enough articles", "found": _found})

    sources = sources[:BRIEFING_EVENT_LIMIT]
    # Compact map sent to the client to resolve [N] citation markers -> links.
    sources_payload = [
        {"n": i + 1, "link": s["link"], "outlet": s.get("outlet"),
         "title": s.get("title"), "n_sources": s.get("n_sources", 1)}
        for i, s in enumerate(sources)
    ]
    sources_json = json.dumps(sources_payload)

    if stream:
        # Stream tokens via SSE (sources first so the client can linkify [N]).
        def sse_stream():
            yield f"data: {json.dumps({'sources': sources_payload})}\n\n"
            full_text = []
            try:
                for chunk in _generate_briefing_stream(sources, view_name, view_desc, hours):
                    full_text.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
                return

            # Cache the completed briefing + its sources map
            briefing = "".join(full_text).strip()
            if briefing:
                generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    uc = get_user_db()
                    uc.execute(
                        "INSERT OR REPLACE INTO briefing_cache "
                        "(cache_key, briefing, article_count, generated_at, sources_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (cache_key, briefing, len(sources), generated_at, sources_json),
                    )
                    uc.commit()
                    uc.close()
                except Exception:
                    pass
            yield f"data: {json.dumps({'done': True, 'article_count': len(sources)})}\n\n"

        return Response(sse_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})

    # Non-streaming fallback
    briefing = _generate_briefing(sources, view_name, view_desc, hours)
    if not briefing:
        return jsonify({"briefing": None, "error": "Briefing generation unavailable"}), 503

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ucon = get_user_db()
    ucon.execute(
        "INSERT OR REPLACE INTO briefing_cache (cache_key, briefing, article_count, generated_at, sources_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (cache_key, briefing, len(sources), generated_at, sources_json),
    )
    ucon.commit()
    ucon.close()

    return jsonify({
        "briefing": briefing,
        "sources": sources_payload,
        "article_count": len(sources),
        "generated_at": generated_at,
        "cached": False,
        "view": view_id or None,
        "hours": hours,
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
