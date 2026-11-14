"""Admin-only review and health surface for social publishing."""

import hmac
import json
import os

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from _paths import DATA_DIR
from social_store import (
    init_social_db, list_candidates, list_pending_notify, list_shadow,
    review_candidate, set_mode, status_summary,
)


bp = Blueprint("social", __name__)


def _admin_only():
    return current_user.is_authenticated and current_user.is_admin


def _decorate(candidate):
    item = dict(candidate)
    try:
        item["members"] = json.loads(item.get("members_json") or "[]")
    except json.JSONDecodeError:
        item["members"] = []
    try:
        item["grounding"] = json.loads(item.get("grounding_json") or "{}")
    except json.JSONDecodeError:
        item["grounding"] = {}
    return item


@bp.route("/admin/social")
@login_required
def social_admin():
    if not _admin_only():
        return redirect(url_for("pages.index"))
    init_social_db()
    summary = status_summary()
    show_expired = request.args.get("expired") == "1"
    shadow = list_shadow()
    candidates = [
        _decorate(c) | {"shadow": shadow.get(c["cluster_id"])}
        for c in list_candidates(120, include_expired=show_expired)
    ]
    credentials_ready = bool(
        (os.environ.get("BLUESKY_HANDLE") and os.environ.get("BLUESKY_APP_PASSWORD"))
        or (DATA_DIR / ".bluesky_bot").exists()
    )
    gateway_ready = bool(
        os.environ.get("GDELT_SOCIAL_GATEWAY_KEY")
        or (DATA_DIR / ".social_gateway_key").exists()
    )
    return render_template(
        "social_admin.html", candidates=candidates, summary=summary,
        credentials_ready=credentials_ready, gateway_ready=gateway_ready,
        show_expired=show_expired,
        expired_count=summary["counts"].get("expired", 0),
    )


@bp.route("/admin/social/<cluster_id>/review", methods=["POST"])
@login_required
def social_review(cluster_id):
    if not _admin_only():
        return jsonify({"error": "forbidden"}), 403
    action = (request.form.get("action") or "").strip()
    explanation = request.form.get("explanation")
    reason = request.form.get("reason")
    ok, message = review_candidate(
        cluster_id, action, current_user.id, explanation=explanation, reason=reason
    )
    flash(message, "success" if ok else "error")
    return redirect(url_for("social.social_admin"))


@bp.route("/admin/social/mode", methods=["POST"])
@login_required
def social_mode():
    if not _admin_only():
        return jsonify({"error": "forbidden"}), 403
    ok, message = set_mode((request.form.get("mode") or "").strip())
    flash(f"Social mode: {message}" if ok else message, "success" if ok else "error")
    return redirect(url_for("social.social_admin"))


@bp.route("/api/social/pending")
def social_pending():
    """Machine-readable pending queue for the beardy notify poller.

    Token-gated (data/.social_notify_token, X-GDELT-Social-Token header) rather
    than session auth — this is called by a timer on the pipeline host, not a browser.
    """
    token_file = DATA_DIR / ".social_notify_token"
    expected = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
    provided = request.headers.get("X-GDELT-Social-Token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        return jsonify({"error": "forbidden"}), 403
    # NOTE: deliberately no init_social_db() here — the poller hits this every
    # 5 min, and per-request schema DDL write txns collided with the
    # publisher's write bursts ("database is locked" at upsert, 2026-11-03).
    return jsonify({"candidates": list_pending_notify()})


@bp.route("/api/social/status")
@login_required
def social_status():
    if not _admin_only():
        return jsonify({"error": "forbidden"}), 403
    return jsonify(status_summary())
