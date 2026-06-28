"""Flask-Login integration for the GDELT dashboard."""

import os
from pathlib import Path

from flask import Flask
from flask_login import LoginManager, UserMixin

from models import get_user_by_id, USERS_DB_PATH

SECRET_KEY_PATH = Path(__file__).resolve().parent.parent / "data" / ".secret_key"


class User(UserMixin):
    def __init__(self, user_dict):
        self._data = user_dict

    def get_id(self):
        return str(self._data["id"])

    @property
    def id(self):
        return self._data["id"]

    @property
    def email(self):
        return self._data["email"]

    @property
    def display_name(self):
        return self._data["display_name"]

    @property
    def is_approved(self):
        return bool(self._data["is_approved"])

    @property
    def is_admin(self):
        return bool(self._data["is_admin"])


def _load_or_create_secret_key() -> str:
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text().strip()
    key = os.urandom(32).hex()
    SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRET_KEY_PATH.write_text(key)
    return key


def init_auth(app: Flask):
    app.secret_key = _load_or_create_secret_key()

    login_manager = LoginManager()
    login_manager.login_view = "auth.login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        data = get_user_by_id(int(user_id))
        if data and data["is_approved"]:
            return User(data)
        return None
