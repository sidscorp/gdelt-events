"""SSR first paint (#6), social metadata (#4) and sitemap (#7) assertions.

Run against a live instance with a warm feed cache:
    BASE=https://gdeltmonitor.com pytest tests/test_ssr.py -v
    BASE=http://rainbow-boi:8016 pytest tests/test_ssr.py -v

The SSR article assertions require the cache to be warm (pipeline/warm_feed.py
runs every ~2 minutes in prod). A cold instance legitimately serves the empty
shell — that's the designed fallback, not a failure of the page.
"""

import os
import re
import time
import urllib.request

BASE = os.environ.get("BASE", "https://gdeltmonitor.com")

VIEW = "geopolitics-conflict"  # warmed at its 24h default by warm_feed.py


def _get(path):
    t0 = time.time()
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "ssr-test"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace"), time.time() - t0


def _meta(html, prop):
    m = re.search(
        rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)"', html)
    return m.group(1) if m else None


# --- issue #6: server-rendered first paint ----------------------------------

def test_home_raw_html_has_articles():
    status, html, _ = _get("/")
    assert status == 200
    assert html.count('<li class="article"') >= 20, \
        "raw HTML of / carries <20 articles — SSR cache miss or regression"


def test_view_raw_html_has_articles():
    _, html, _ = _get(f"/?view={VIEW}")
    assert html.count('<li class="article"') >= 20


def test_home_has_nonempty_heading():
    _, html, _ = _get("/")
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    assert m and m.group(1).strip()


def test_ssr_ttfb_budget():
    _get("/")  # ensure warm
    _, _, dt = _get("/")
    assert dt < 2.0, f"warm-cache SSR response took {dt:.2f}s"


def test_ssr_marks_hydration_contract():
    _, html, _ = _get("/")
    assert 'data-ssr="1"' in html
    assert 'data-snap-key="snap:|3|1|importance"' in html


# --- issue #4: social + search metadata -------------------------------------

REQUIRED_META = ["description", "og:title", "og:description", "og:image",
                 "og:url", "og:type", "twitter:card", "twitter:title"]


def test_meta_tags_on_home():
    _, html, _ = _get("/")
    for prop in REQUIRED_META:
        assert _meta(html, prop), f"missing meta {prop} on /"
    assert '<link rel="canonical" href="https://gdeltmonitor.com/"' in html
    assert _meta(html, "twitter:card") == "summary_large_image"


def test_meta_tags_on_view_and_titles_differ():
    _, home, _ = _get("/")
    _, view, _ = _get(f"/?view={VIEW}")
    for prop in REQUIRED_META:
        assert _meta(view, prop), f"missing meta {prop} on view"
    assert _meta(home, "og:title") != _meta(view, "og:title")
    assert f"/?view={VIEW}" in _meta(view, "og:url")


def test_title_reflects_view():
    _, html, _ = _get(f"/?view={VIEW}")
    m = re.search(r"<title>([^<]+)</title>", html)
    assert m and "GDELT Monitor" in m.group(1) and "Geopolitics" in m.group(1)


def test_og_image_serves():
    status, _, _ = _get("/static/og-card.png")
    assert status == 200


# --- issue #7: sitemap + robots ---------------------------------------------

def test_sitemap_valid_and_populated():
    status, xml, _ = _get("/sitemap.xml")
    assert status == 200
    assert xml.lstrip().startswith("<?xml")
    assert "<urlset" in xml
    locs = xml.count("<loc>")
    assert locs >= 20, f"sitemap has only {locs} URLs"
    assert f"<loc>https://gdeltmonitor.com/?view={VIEW}</loc>" in xml


def test_robots_references_sitemap():
    status, txt, _ = _get("/robots.txt")
    assert status == 200
    assert "Sitemap: https://gdeltmonitor.com/sitemap.xml" in txt
