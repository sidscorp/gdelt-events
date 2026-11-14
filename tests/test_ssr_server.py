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


def test_previous_cycle_payload_covers_the_post_ingest_gap(client, monkeypatch):
    """New data is the real invalidator: the key carries data_version, so every
    ingest blanks the cache until warm_feed refills it. The first paint must not
    fall back to the client shell in that gap."""
    import json as _json
    import time as _time

    import articles

    articles._feed_cache.clear()
    with app_module.app.test_request_context(
        "/api/articles?hours=3&match_types=legal&en_only=1&page=1&per_page=50"
    ):
        previous_key = articles._feed_cache_key()
    articles._feed_cache[previous_key] = (
        _time.time() + articles._FEED_TTL_S,
        _json.dumps(_feed(2)).encode(),
    )

    monkeypatch.setattr(articles, "_data_version", lambda: "9999999999")
    html = client.get("/").get_data(as_text=True)
    assert html.count('<li class="article">') == 2
    assert 'data-ssr="1"' in html


def test_stale_payload_is_not_served_forever(client, monkeypatch):
    """The fallback is bounded to one ingest cycle, so a render cannot outlive
    the data it describes."""
    import json as _json
    import time as _time

    import articles

    articles._feed_cache.clear()
    with app_module.app.test_request_context(
        "/api/articles?hours=3&match_types=legal&en_only=1&page=1&per_page=50"
    ):
        previous_key = articles._feed_cache_key()
    too_old = _time.time() + articles._FEED_TTL_S - (pages._SSR_STALE_MAX_S + 300)
    articles._feed_cache[previous_key] = (too_old, _json.dumps(_feed(2)).encode())

    monkeypatch.setattr(articles, "_data_version", lambda: "9999999999")
    html = client.get("/").get_data(as_text=True)
    assert '<li class="article">' not in html
