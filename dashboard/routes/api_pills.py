"""Custom pill API routes."""

from pathlib import Path

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

from db import get_db
from models import (
    get_user_pills, create_pill, create_semantic_pill,
    get_pill, get_pill_job_status, delete_pill,
)

bp = Blueprint("api_pills", __name__)


@bp.route("/api/pills", methods=["GET"])
@login_required
def api_pills_list():
    pills = get_user_pills(current_user.id)
    return jsonify({"pills": pills})


@bp.route("/api/pills", methods=["POST"])
@login_required
def api_pills_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    pill_type = data.get("pill_type", "keyword")

    if not name or len(name) > 100:
        return jsonify({"error": "Name required (max 100 chars)"}), 400

    # Self-hosting guardrail: bounded pills per user (each pill = one query
    # vector + ~100B per tagged article; the cap keeps worst-case trivial).
    if len(get_user_pills(current_user.id)) >= 10:
        return jsonify({"error": "Pill limit reached (10 per user) — delete one first"}), 400

    if pill_type == "semantic":
        description = (data.get("description") or "").strip()
        if not description or len(description) < 10:
            return jsonify({"error": "Description must be at least 10 characters"}), 400
        if len(description) > 2000:
            return jsonify({"error": "Description max 2000 characters"}), 400
        threshold = data.get("similarity_threshold", 0.55)
        try:
            # 0.45 floor: below that nomic cosine matches are topic soup.
            threshold = max(0.45, min(0.8, float(threshold)))
        except (TypeError, ValueError):
            threshold = 0.55
        scan_days = data.get("scan_days", 60)
        try:
            scan_days = max(1, min(60, int(scan_days)))
        except (TypeError, ValueError):
            scan_days = 60

        # Embed the description via rainbow-boi
        import sys, struct
        _pipeline = str(Path(__file__).resolve().parent.parent / "pipeline")
        if _pipeline not in sys.path:
            sys.path.insert(0, _pipeline)
        try:
            from embedder import embed_query
            vec = embed_query(description)
            embedding_blob = struct.pack(f"{len(vec)}f", *vec)
        except Exception as e:
            return jsonify({"error": f"Embedding service error: {e}"}), 503

        pill_id = create_semantic_pill(
            current_user.id, name, description, embedding_blob, threshold,
            scan_days,
        )
        return jsonify({"pill_id": pill_id, "status": "queued", "pill_type": "semantic"}), 201

    # Keyword pill (default)
    keywords_raw = data.get("keywords", "")
    scan_desc = data.get("scan_description", True)

    if isinstance(keywords_raw, str):
        keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
    elif isinstance(keywords_raw, list):
        keywords = [str(k).strip().lower() for k in keywords_raw if str(k).strip()]
    else:
        keywords = []

    if len(keywords) < 2:
        return jsonify({"error": "At least 2 keywords required"}), 400
    if len(keywords) > 200:
        return jsonify({"error": "Max 200 keywords"}), 400

    pill_id = create_pill(current_user.id, name, keywords, scan_desc)
    return jsonify({"pill_id": pill_id, "status": "queued", "pill_type": "keyword"}), 201


@bp.route("/api/pills/<pill_id>/status")
@login_required
def api_pill_status(pill_id):
    pill = get_pill(pill_id)
    if not pill or pill["user_id"] != current_user.id:
        return jsonify({"error": "not found"}), 404
    job = get_pill_job_status(pill_id)
    return jsonify({
        "id": pill_id,
        "name": pill["name"],
        "status": job["status"] if job else "unknown",
        "progress_pct": job["progress_pct"] if job else 0,
        "rows_scanned": job["rows_scanned"] if job else 0,
        "rows_matched": job["rows_matched"] if job else 0,
        "elapsed_seconds": job["elapsed_seconds"] if job else 0,
        "article_count": pill["article_count"],
    })


@bp.route("/api/pills/<pill_id>", methods=["DELETE"])
@login_required
def api_pills_delete(pill_id):
    pill = get_pill(pill_id)
    if not pill:
        return jsonify({"error": "not found"}), 404
    if pill["user_id"] != current_user.id and not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    # Delete article_tags for this custom pill from DuckDB
    try:
        con = get_db()
        if con:
            con.execute(
                "DELETE FROM article_tags WHERE category = ?",
                [f"custom_{pill_id}"],
            )
            con.close()
    except Exception:
        pass
    delete_pill(pill_id)
    return jsonify({"deleted": pill_id})
