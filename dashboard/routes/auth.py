"""Auth + admin routes: login, register, account, portal, admin user mgmt."""

from flask import (
    Blueprint, render_template, request, jsonify,
    redirect, url_for, flash,
)
from flask_login import login_user, logout_user, login_required, current_user

from models import (
    create_user, authenticate, email_exists,
    get_user_pills, list_users, approve_user, reject_user,
)
from auth import User

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        user_data = authenticate(email, password)
        if not user_data:
            flash("Invalid email or password.", "error")
            return render_template("login.html")
        if not user_data["is_approved"]:
            return redirect(url_for("auth.pending"))
        login_user(User(user_data), remember=True)
        return redirect(url_for("pages.index"))
    return render_template("login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")
        if not email or not display_name or len(password) < 8:
            flash("All fields required. Password must be at least 8 characters.", "error")
            return render_template("register.html")
        if email_exists(email):
            flash("An account with this email already exists.", "error")
            return render_template("register.html")
        uid = create_user(email, display_name, password)
        user_data = authenticate(email, password)
        if user_data and user_data["is_approved"]:
            login_user(User(user_data), remember=True)
            return redirect(url_for("pages.index"))
        return redirect(url_for("auth.pending"))
    return render_template("register.html")


@bp.route("/pending")
def pending():
    return render_template("pending.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("pages.index"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "change_password":
            current = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            if len(new_pw) < 8:
                flash("New password must be at least 8 characters.", "error")
            else:
                from models import get_user_db
                from werkzeug.security import check_password_hash, generate_password_hash
                db = get_user_db()
                row = db.execute("SELECT password_hash FROM users WHERE id=?",
                                 (current_user.id,)).fetchone()
                if not row or not check_password_hash(row["password_hash"], current):
                    flash("Current password is incorrect.", "error")
                else:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (generate_password_hash(new_pw), current_user.id))
                    db.commit()
                    flash("Password updated.", "success")
                db.close()
        elif action == "change_name":
            new_name = request.form.get("display_name", "").strip()
            if new_name:
                from models import get_user_db
                db = get_user_db()
                db.execute("UPDATE users SET display_name=? WHERE id=?",
                           (new_name, current_user.id))
                db.commit()
                db.close()
                flash("Display name updated.", "success")
    return render_template("account.html")


@bp.route("/portal")
@login_required
def portal():
    pills = get_user_pills(current_user.id)
    return render_template("portal.html", pills=pills, is_admin=current_user.is_admin)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@bp.route("/admin/users")
@login_required
def admin_users():
    if not current_user.is_admin:
        return redirect(url_for("pages.index"))
    users = list_users()
    return render_template("admin.html", users=users)


@bp.route("/admin/users/<int:user_id>/approve", methods=["POST"])
@login_required
def admin_approve(user_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    approve_user(user_id)
    return redirect(url_for("auth.admin_users"))


@bp.route("/admin/users/<int:user_id>/reject", methods=["POST"])
@login_required
def admin_reject(user_id):
    if not current_user.is_admin:
        return jsonify({"error": "forbidden"}), 403
    reject_user(user_id)
    return redirect(url_for("auth.admin_users"))
