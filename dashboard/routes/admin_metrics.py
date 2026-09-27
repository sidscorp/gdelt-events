"""Admin-only metrics: readers, briefing quality, spend and pipeline health."""

from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

import metrics


bp = Blueprint("admin_metrics", __name__)


def _admin_only():
    return current_user.is_authenticated and current_user.is_admin


@bp.route("/admin/metrics")
@login_required
def admin_metrics():
    if not _admin_only():
        return redirect(url_for("pages.index"))
    data = metrics.collect(fresh=request.args.get("fresh") == "1")
    return render_template("admin_metrics.html", m=data)


@bp.route("/admin/metrics.json")
@login_required
def admin_metrics_json():
    if not _admin_only():
        return jsonify({"error": "forbidden"}), 403
    return jsonify(metrics.collect(fresh=request.args.get("fresh") == "1"))
