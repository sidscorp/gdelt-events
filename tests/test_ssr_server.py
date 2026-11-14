"""Offline SSR contract tests — no network, no DuckDB, no warm cache.

`tests/test_ssr.py` is the live layer: it proves production serves a *warmed*
cache over HTTP. This file proves the server-side render itself, which CI can
gate without a running site: the article markup, the per-view title/canonical/
og:url tags, and the deliberate fall-back to the client shell for the shapes the
cache is never warmed for (`_ssr_feed`'s docstring, issue #6).

Both data hooks are injected, so nothing here depends on news volume, ingest
freshness or a populated database — those belong to the live layer.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# dashboard/app.py opens the SQLite stores and creates log dirs at import time.
# Point it at a throwaway data dir first so the suite never touches real data.
_TMP_DATA = Path(tempfile.mkdtemp(prefix="gdelt-ssr-test-"))
os.environ.setdefault("GDELT_DATA_DIR", str(_TMP_DATA))

_DASH = Path(__file__).resolve().parent.parent / "dashboard"
sys.path.insert(0, str(_DASH))

import app as app_module          # noqa: E402
import routes.pages as pages      # noqa: E402
from views import VIEWS           # noqa: E402

CANON = "https://gdeltmonitor.com"


def _article(i: int) -> dict:
    return {
        "title": f"Server rendered story {i}",
        "url": f"https://example.test/story-{i}",
        "source": f"Outlet {i}",
        "time_ago": f"{i}h",
        "description": None,
        "image": None,
        "variant_count": 1,
        "event_url": None,
    }


def _feed(n: int = 3) -> dict:
    return {"articles": [_article(i) for i in range(1, n + 1)], "total": n}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pages, "_briefing_html", lambda *a, **k: (None, None))
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


@pytest.fixture
def warm(monkeypatch):
    """Pretend the feed cache holds page 1 — the SSR hit path."""
    monkeypatch.setattr(pages, "_ssr_feed", lambda *a, **k: _feed(3))
    return pages


def test_home_renders_every_article_server_side(client, warm):
    html = client.get("/").get_data(as_text=True)
    assert html.count('<li class="article">') == 3
    for i in (1, 2, 3):
        assert f"Server rendered story {i}" in html
    # the client hydrates over this markup, so it must be flagged and keyed
    assert 'data-ssr="1"' in html
    assert "data-snap-key=" in html


def test_home_meta_tags_for_shareability(client, warm):
    html = client.get("/").get_data(as_text=True)
    assert f'<link rel="canonical" href="{CANON}/"' in html
    assert f'property="og:url" content="{CANON}/"' in html
    assert "og:image" in html


def test_view_page_has_its_own_title_and_canonical(client, warm):
    view = VIEWS[0]
    home = client.get("/").get_data(as_text=True)
    html = client.get(f"/?view={view['id']}").get_data(as_text=True)

    assert view["name"] in html
    assert f'<link rel="canonical" href="{CANON}/?view={view["id"]}"' in html
    assert f'property="og:url" content="{CANON}/?view={view["id"]}"' in html
    assert html.count('<li class="article">') == 3
    # titles must differ, or every view shares one search/social identity
    assert _title(home) != _title(html)


def test_non_ssr_shapes_fall_back_to_the_client_shell(client, monkeypatch):
    """Anything the cache is not warmed for must not trigger a cache lookup."""
    def explode(*a, **k):
        raise AssertionError("_ssr_feed must not run for a non-SSR shape")

    monkeypatch.setattr(pages, "_ssr_feed", explode)
    monkeypatch.setattr(pages, "_briefing_html", lambda *a, **k: (None, None))

    html = client.get("/?en_only=0").get_data(as_text=True)
    assert '<li class="article">' not in html
    assert 'data-ssr="1"' not in html


def test_server_rendered_briefing_appears_in_first_paint(client, monkeypatch):
    monkeypatch.setattr(pages, "_ssr_feed", lambda *a, **k: _feed(1))
    monkeypatch.setattr(
        pages, "_briefing_html",
        lambda *a, **k: ("<p>Server rendered briefing body.</p>", "2026-11-14 12:00:00"),
    )
    html = client.get("/").get_data(as_text=True)
    assert "Server rendered briefing body." in html


def _title(html: str) -> str:
    start = html.find("<title>")
    end = html.find("</title>", start)
    return html[start + 7:end] if start >= 0 and end > start else ""
