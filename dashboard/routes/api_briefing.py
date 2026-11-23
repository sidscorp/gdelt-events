"""AI briefing + FDA events API routes."""

import json
import threading
import hashlib
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify
from briefing_access import parse_key, worker_authorized

import time as _time

from db import get_db
from briefing import (
    BRIEFING_FRESH_S, BRIEFING_EVENT_LIMIT, BRIEFING_MODEL, fresh_s,
    _build_briefing_prompt, _fetch_briefing_events, _select_events,
    _generate_briefing, _generate_briefing_stream,
    _normalize_briefing,
    BriefingGenerationError, get_briefing_attempt, record_briefing_attempt,
    get_threads, record_briefing_history, update_threads_async,
    should_update_threads,
)

bp = Blueprint("api_briefing", __name__)


def _freshness_fields(view_id, hours, generated_at):
    """Attempt state + schedule for the reader's freshness line.

    ``next_eligible_at`` is deliberately the next *scheduled* prewarm run that
    is allowed to refresh this combo — not ``generated_at + ttl``. The ttl only
    says when the cache stops counting as fresh; the prewarm slot is when an
    update can actually happen. On 2026-11-23 the bar derived its countdown from
    the ttl alone and promised "any moment" across an 8-hour overnight gap the
    schedule could never cover.

    Returns nulls rather than failing: an install with no recorded attempts, or
    an unreadable schedule, must still serve the briefing.
    """
    attempt = get_briefing_attempt(view_id, hours) or {}
    next_at = None
    if generated_at:
        try:
            from pipeline.prewarm_briefings import next_run_at
            eligible_at = datetime.strptime(generated_at, "%Y-%m-%d %H:%M:%S") \
                + timedelta(seconds=fresh_s(hours))
            nxt = next_run_at(hours, max(datetime.utcnow(), eligible_at))
            if nxt is not None:
                next_at = nxt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            next_at = None
    return {
        "last_attempt_at": attempt.get("last_attempt_at"),
        "last_attempt_outcome": attempt.get("last_attempt_outcome"),
        "consecutive_failures": int(attempt.get("consecutive_failures") or 0),
        "next_eligible_at": next_at,
    }

# One scheduled generation at a time in the single Waitress process. Public
# requests never enter this guard or invoke the model. Multiple server processes
# require a shared lock before enabling generation on them.
_regen_lock = threading.Lock()
_regen_in_flight: set = set()


def _try_start_regen(cache_key: str) -> bool:
    """Acquire regeneration slot. Returns True if this caller should generate."""
    with _regen_lock:
        if _regen_in_flight:
            return False
        _regen_in_flight.add(cache_key)
        return True


def _finish_regen(cache_key: str):
    """Release regeneration slot."""
    with _regen_lock:
        _regen_in_flight.discard(cache_key)


def _editor_pass(candidates, view_name, view_desc, hours, threads):
    """Editor selection + the citation payload.

    ``sources_payload`` deliberately carries ALL candidates, numbered 1..N, not
    just the chosen ones: the client maps [N] -> sources[N-1], and the writer
    cites each story by its candidate number, so the persisted array must be
    the same index space the writer was shown. It also lets the info panel show
    what was passed over.
    """
    selected, verdicts = _select_events(candidates, view_name, view_desc, hours, threads)
    payload = [{"n": i + 1, "link": s["link"], "outlet": s.get("outlet"),
                "title": s.get("title"), "n_sources": s.get("n_sources", 1)}
               for i, s in enumerate(candidates)]
    chosen_ns = {s.get("n") for s in selected}
    for row in payload:
        row["chosen"] = row["n"] in chosen_ns
    editor_meta = {
        "n_candidates": len(candidates),
        "n_chosen": len(selected),
        "verdicts": verdicts,
        "fell_back": not verdicts,
    }
    return selected, payload, editor_meta


@bp.route("/api/briefing", methods=["GET", "POST"])
def api_briefing():
    """Cache-only public JSON/SSE reads; authenticated scheduled POST generation."""
    from flask import Response

    try:
        view_id, hours = parse_key(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    stream = request.args.get("stream", "0") == "1"
    # Two non-visitor modes, deliberately different:
    #   refresh=1  force regeneration, ignoring freshness (manual / debugging)
    #   prewarm=1  scheduled warm — regenerate ONLY if stale for this window
    # The scheduled job uses prewarm=1 so fresh_s() governs it. With refresh=1 it
    # rewrote every combo on every run, which for a 30-day briefing (fresh 24h)
    # meant five regenerations a day of content that had barely moved.
    refresh = request.args.get("refresh") == "1"
    prewarm = request.args.get("prewarm") == "1"

    is_worker = request.method == "POST"
    if (refresh or prewarm or is_worker) and (not is_worker or not worker_authorized(request.headers)):
        return jsonify({"error": "Scheduled generation authorization required"}), 403
    if is_worker:
        from pipeline.prewarm_briefings import CURATED_VIEWS
        # All six UI time windows are valid generation targets (2026-11-09 —
        # was CURATED_HOURS only, which rejected the 6h/3d/7d/30d windows the
        # widened prewarm schedule now warms).
        ALLOWED_HOURS = (3, 6, 24, 72, 168, 720)
        if stream or not (refresh or prewarm) or view_id not in CURATED_VIEWS or hours not in ALLOWED_HOURS:
            return jsonify({"error": "Unsupported scheduled generation request"}), 400

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

    if not is_worker:
        # GET is always cache-only, including misses, stale entries, and SSE.
        # No ambient browser credentials can grant generation authority.
        freshness = _freshness_fields(view_id, hours,
                                      cached["generated_at"] if cached else None)
        payload = {
            "briefing": cached["briefing"] if cached else None,
            "sources": cached_sources,
            "article_count": cached["article_count"] if cached else 0,
            "generated_at": cached["generated_at"] if cached else None,
            "cache_ttl_s": fresh_s(hours), "cached": bool(cached),
            "stale": is_stale, "view": view_id or None, "hours": hours,
            **freshness,
        }
        if not cached:
            payload["error"] = "No cached briefing; scheduled update pending"
        if not stream:
            return jsonify(payload), (200 if cached else 503)
        def cached_stream():
            if cached:
                yield f"data: {json.dumps({'sources': cached_sources})}\n\n"
                # The attempt fields ride the first paint too: phase 1 can sit on
                # screen for the whole (slow) regeneration, and the bar must be
                # able to say "last attempt failed 4m ago" during it.
                yield f"data: {json.dumps({'text': cached['briefing'], 'cached': True, 'done': False, 'stale': is_stale, 'generated_at': cached['generated_at'], 'cache_ttl_s': fresh_s(hours), **freshness})}\n\n"
            terminal = {k: v for k, v in payload.items() if k not in ('briefing', 'sources')}
            terminal["done"] = True
            yield f"data: {json.dumps(terminal)}\n\n"
        return Response(cached_stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})

    needs_regen = is_stale or refresh
    should_regen = needs_regen and _try_start_regen(cache_key)

    # ── non-streaming path ──
    if not stream:
        def _record(outcome, detail=None):
            # Every attempt is recorded, including the ones that never reached
            # the model: "busy" and "insufficient articles" are exactly the
            # outcomes that were invisible while the bar kept promising an
            # update.
            record_briefing_attempt(view_id, hours, outcome,
                                    trigger=history_trigger, detail=detail)

        def _unrefreshed(outcome, detail=None, message="Briefing generation unavailable",
                         status=503, extra=None):
            """Serve what we have and record why it was not refreshed."""
            _record(outcome, detail)
            fields = _freshness_fields(view_id, hours,
                                       cached["generated_at"] if cached else None)
            if cached:
                return jsonify({
                    "briefing": cached["briefing"],
                    "sources": cached_sources,
                    "article_count": cached["article_count"],
                    "generated_at": cached["generated_at"],
                    "cache_ttl_s": fresh_s(hours),
                    "cached": True,
                    "stale": True,
                    "view": view_id or None,
                    "hours": hours,
                    **fields,
                })
            body = {"briefing": None, "error": message, **fields}
            if extra:
                body.update(extra)
            return jsonify(body), status

        if cached and not needs_regen:
            # Fresh for this window: no model call, but the attempt is still an
            # observation (this is the "skip" the prewarm log counts).
            _record("skipped_fresh")
            return jsonify({
                "briefing": cached["briefing"],
                "sources": cached_sources,
                "article_count": cached["article_count"],
                "generated_at": cached["generated_at"],
                "cache_ttl_s": fresh_s(hours),
                "cached": True,
                "view": view_id or None,
                "hours": hours,
                **_freshness_fields(view_id, hours, cached["generated_at"]),
            })
        if not should_regen:
            # Another request is already regenerating; just return stale content
            return _unrefreshed("failed:busy", "another generation was in flight",
                                message="Generation in progress, no cache yet")

        prev = None
        if cached:
            try:
                prev = {"briefing": cached["briefing"], "generated_at": cached["generated_at"],
                        "sources": cached_sources}
            except Exception:
                prev = None
        try:
            threads = get_threads(cache_key)
            try:
                sources, view_name, view_desc = _fetch_briefing_events(view_id, hours)
            except BriefingGenerationError as exc:
                # A window we could not read is not a quiet window; name it.
                return _unrefreshed(f"failed:{exc.code}", exc.detail)
            if len(sources) < 2:
                return _unrefreshed("failed:insufficient_articles",
                                    f"{len(sources)} candidate articles in the window",
                                    message="Not enough articles", status=200,
                                    extra={"found": len(sources)})

            candidates = sources
            sources, sources_payload, editor_meta = _editor_pass(
                candidates, view_name, view_desc, hours, threads)
            sources_json = json.dumps(sources_payload)
            prompt = _build_briefing_prompt(sources, view_name, view_desc, hours,
                                            prev=prev, threads=threads)
            gen_t0 = _time.time()

            try:
                briefing = _normalize_briefing(_generate_briefing(sources, view_name, view_desc, hours,
                                                                   prev=prev, threads=threads, prompt=prompt),
                                               view_name, hours)
            except BriefingGenerationError as exc:
                return _unrefreshed(f"failed:{exc.code}", exc.detail)
            if not briefing:
                return _unrefreshed("failed:empty_briefing", "normalised to empty text")

            generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            meta_json = json.dumps({
                "model": BRIEFING_MODEL, "prompt": prompt, "n_sources": len(sources),
                "editor_selection": editor_meta,
                "continuity_titles": [
                    (s.get("title") or "")[:110] for s in (prev or {}).get("sources", [])[:8]
                ] if prev else [],
                "threads_used": [
                    {"title": t.get("title"), "first_seen": t.get("first_seen"),
                     "summary": t.get("summary")}
                    for t in (threads or []) if (t.get("status") or "active") == "active"
                ],
                "elapsed_s": round(_time.time() - gen_t0, 2),
                "briefing_chars": len(briefing), "cache_ttl_s": fresh_s(hours),
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
            _record("ok")
            record_briefing_history(cache_key, view_id or "_all", hours, briefing,
                                    sources_json, len(sources), meta_json, generated_at,
                                    trigger=history_trigger)
            if should_update_threads(cache_key, history_trigger):
                update_threads_async(cache_key, threads, briefing)
            return jsonify({
                "briefing": briefing, "sources": sources_payload,
                "article_count": len(sources), "generated_at": generated_at,
                "cache_ttl_s": fresh_s(hours),
                "cached": False, "view": view_id or None, "hours": hours,
                **_freshness_fields(view_id, hours, generated_at),
            })
        finally:
            _finish_regen(cache_key)



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


@bp.route("/api/briefing_history")
def api_briefing_history():
    """Snapshot list for one (view, hours) combo — powers the briefing timeline.

    Pure metadata (id, time, count) so the strip is cheap; the full briefing
    body comes from /api/briefing_history/<id> on demand. History is recorded
    since 2026-07-26 and is dominated by prewarmed combos — that is why the
    UI lists rather than implying continuous coverage.
    """
    raw_view = (request.args.get("view") or "").strip()
    view_id = raw_view or "_all"   # global briefing is stored under '_all'
    try:
        hours = max(1, min(720, int(request.args.get("hours", 24) or 24)))
    except (TypeError, ValueError):
        hours = 24

    try:
        from models import get_user_db
        con = get_user_db()
        try:
            rows = con.execute(
                "SELECT id, generated_at, article_count, trigger "
                "FROM briefing_history WHERE view_id = ? AND hours = ? "
                "ORDER BY generated_at DESC LIMIT 60",
                (view_id, hours),
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        return jsonify({"snapshots": [], "error": str(e)}), 500

    snapshots = [
        {"id": r["id"], "generated_at": r["generated_at"],
         "article_count": r["article_count"], "trigger": r["trigger"]}
        for r in rows
    ]
    return jsonify({"view": raw_view, "hours": hours,
                    "snapshots": snapshots, "total": len(snapshots)})


@bp.route("/api/briefing_history/<int:bid>")
def api_briefing_history_item(bid):
    """One stored briefing, for the timeline view."""
    try:
        from models import get_user_db
        con = get_user_db()
        try:
            row = con.execute(
                "SELECT view_id, hours, generated_at, briefing, article_count, sources_json "
                "FROM briefing_history WHERE id = ?",
                (bid,),
            ).fetchone()
        finally:
            con.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if row is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": bid,
        "view": "" if row["view_id"] == "_all" else row["view_id"],
        "hours": row["hours"],
        "generated_at": row["generated_at"],
        "briefing": row["briefing"],
        "article_count": row["article_count"],
        "sources": json.loads(row["sources_json"]) if row["sources_json"] else [],
    })


# --- Pageview tracking (privacy-safe: daily-salted IP hash, no raw IP) ---

PAGEVIEW_EVENT_TYPES = ("pageview", "feed_expand")


def _ip_hash():
    ip = (request.headers.get("CF-Connecting-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "?")
    # Static salt: same IP always maps to the same hash, so we can count
    # returning visitors across days. Never stores raw IP.
    return hashlib.sha256(f"gdelt-pageview:{ip}".encode()).hexdigest()[:32]


@bp.route("/api/pageview", methods=["POST"])
def api_pageview():
    """Record a lightweight, privacy-safe pageview. Fire-and-forget — the
    client sends this via sendBeacon and never waits for a response."""
    from models import get_user_db
    payload = request.get_json(force=True, silent=True) or {}
    try:
        uc = get_user_db()
        uc.execute("""CREATE TABLE IF NOT EXISTS pageview_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT (datetime('now')),
            path TEXT,
            view_id TEXT,
            hours INTEGER,
            briefing_key TEXT,
            ip_hash TEXT,
            ua TEXT,
            screen_w INTEGER,
            referrer TEXT,
            country TEXT,
            region TEXT,
            city TEXT,
            timezone TEXT
        )""")
        cols = {r[1] for r in uc.execute("PRAGMA table_info(pageview_log)")}
        for col, sql in [
            ("country", "ALTER TABLE pageview_log ADD COLUMN country TEXT"),
            ("region", "ALTER TABLE pageview_log ADD COLUMN region TEXT"),
            ("city", "ALTER TABLE pageview_log ADD COLUMN city TEXT"),
            ("timezone", "ALTER TABLE pageview_log ADD COLUMN timezone TEXT"),
        ]:
            if col not in cols:
                uc.execute(sql)
        if "event_type" not in cols:
            # dashboard.js beaconEvent() has sent {event_type: 'feed_expand'} here
            # since 09-19 and each one was stored as a pageview. Those beacons carry
            # no screen_w and real pageviews always do (0 such rows before 09-19),
            # so relabel them once when the column arrives.
            uc.execute("ALTER TABLE pageview_log ADD COLUMN event_type TEXT DEFAULT 'pageview'")
            uc.execute("UPDATE pageview_log SET event_type = 'feed_expand' "
                       "WHERE screen_w IS NULL AND ts >= '2026-09-19'")
        event_type = payload.get("event_type")
        event_type = event_type if event_type in PAGEVIEW_EVENT_TYPES else "pageview"
        uc.execute(
            "INSERT INTO pageview_log "
            "(ts, path, view_id, hours, briefing_key, ip_hash, ua, screen_w, referrer, country, region, city, timezone, event_type) "
            "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (payload.get("path") or "")[:300],
                (payload.get("view_id") or "")[:80],
                payload.get("hours"),
                (payload.get("briefing_key") or "")[:120],
                _ip_hash(),
                (request.headers.get("User-Agent") or "")[:200],
                payload.get("screen_w"),
                (payload.get("referrer") or "")[:300],
                (request.headers.get("CF-IPCountry") or "")[:10],
                (request.headers.get("CF-Region-Code") or "")[:20],
                (request.headers.get("CF-IPCity") or "")[:60],
                (payload.get("timezone") or "")[:60],
                event_type,
            ),
        )
        uc.execute("DELETE FROM pageview_log WHERE id < "
                   "(SELECT max(id) - 100000 FROM pageview_log)")
        uc.commit()
        uc.close()
    except Exception:
        pass  # never affect the user's experience
    return jsonify({"ok": True})
