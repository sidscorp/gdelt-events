"""Social signal selection, formatting, workflow, and safety contracts."""

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest


@pytest.fixture()
def social_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("GDELT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GDELT_SOCIAL_DB", str(tmp_path / "social.db"))
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "dashboard"))
    monkeypatch.syspath_prepend(str(root / "pipeline"))
    for name in ["_paths", "config", "social_store", "social_publisher"]:
        sys.modules.pop(name, None)
    store = importlib.import_module("social_store")
    publisher = importlib.import_module("social_publisher")
    store.init_social_db()
    return store, publisher, tmp_path


def _candidate(now=None):
    now = now or datetime.now(timezone.utc)
    latest = int(now.strftime("%Y%m%d%H%M%S"))
    first = int((now - timedelta(hours=2)).strftime("%Y%m%d%H%M%S"))
    return {
        "cluster_id": "c0123456789abcde",
        "topic_id": "cyber-attacks",
        "topic_name": "Cybersecurity",
        "title": "Three hospitals report a shared ransomware disruption",
        "title_key": "three hospitals report a shared ransomware disruption",
        "source_count": 4,
        "coverage_span_hours": 2.0,
        "first_seen": first,
        "latest_seen": latest,
        "importance_score": 0.72,
        "member_tags": 3,
        "event_url": "https://gdeltmonitor.com/event/c0123456789abcde?src=bsky",
        "members": [],
        "explanation": "Three hospitals reported disruption from the same ransomware incident.",
        "explanation_kind": "facts",
        "grounding": {"supported": False, "reason": "test fallback"},
        "post_text": "preview",
        "status": "pending",
        "expires_at": (now + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
    }


def test_post_format_is_bounded_and_link_facet_uses_utf8_bytes(social_modules):
    _, publisher, _ = social_modules
    candidate = _candidate()
    explanation = "Équipes report a disruption. " + ("Evidence remains limited. " * 20)
    text = publisher.build_post_text(candidate, explanation)
    assert len(text) <= publisher.POST_LIMIT
    assert text.endswith(candidate["event_url"])
    facet = publisher._link_facet(text, candidate["event_url"])
    prefix = text[: text.index(candidate["event_url"])]
    assert facet["index"]["byteStart"] == len(prefix.encode("utf-8"))
    assert facet["index"]["byteEnd"] == len(text.encode("utf-8"))


def test_deterministic_grounding_rejects_predictions_and_new_numbers(social_modules):
    _, publisher, _ = social_modules
    sources = "The agency issued a recall covering 12 devices."
    assert publisher._deterministic_grounding(
        "The recall covers 12 devices in the filing.", sources
    )[0]
    assert not publisher._deterministic_grounding(
        "The recall will likely pressure markets.", sources
    )[0]
    assert not publisher._deterministic_grounding(
        "The recall covers 30 devices in the filing.", sources
    )[0]


def test_title_dedupe_and_domain_count(social_modules):
    _, publisher, _ = social_modules
    assert publisher._near_duplicate(
        "hospital network reports ransomware disruption",
        "hospital network reports major ransomware disruption",
    )
    members = [
        {"domain": "www.example.com", "url": "https://example.com/a"},
        {"domain": "example.com", "url": "https://example.com/b"},
        {"url": "https://second.test/c"},
    ]
    assert publisher._source_count(members) == 2


def test_review_workflow_and_idempotent_candidate_insert(social_modules):
    store, publisher, _ = social_modules
    candidate = _candidate()
    assert store.upsert_candidate(candidate)
    assert not store.upsert_candidate(candidate)
    ok, state = store.review_candidate(
        candidate["cluster_id"], "approve", 1,
        explanation="Hospitals reported a shared ransomware disruption.",
    )
    assert ok and state == "approved"
    row = store.get_candidate(candidate["cluster_id"])
    assert row["review_decision"] == "approve"
    assert row["explanation_kind"] == "edited"
    assert publisher._record_key(candidate["cluster_id"]) == "evt-c0123456789abcde"


def test_auto_mode_stays_locked_before_calibration(social_modules):
    store, _, _ = social_modules
    ok, message = store.set_mode("auto")
    assert not ok
    assert "locked" in message
    assert store.setting("mode") == "review"


def test_scan_builds_one_ranked_operational_candidate(social_modules):
    _, publisher, tmp_path = social_modules
    now = datetime.now(timezone.utc)
    latest = int(now.strftime("%Y%m%d%H%M%S"))
    first = int((now - timedelta(hours=2)).strftime("%Y%m%d%H%M%S"))
    members = [
        {"url": f"https://source{i}.test/a", "domain": f"source{i}.test",
         "outlet": f"Source {i}", "title": "Hospitals report ransomware disruption",
         "desc": "The hospital network reported a ransomware disruption.",
         "crawled_at": latest}
        for i in range(1, 5)
    ]
    con = duckdb.connect(str(tmp_path / "gdelt.duckdb"))
    con.execute("CREATE TABLE clusters(cluster_id VARCHAR, title VARCHAR, first_seen BIGINT, latest_seen BIGINT, members_json VARCHAR, status VARCHAR)")
    con.execute("CREATE TABLE cluster_members(article_url VARCHAR, cluster_id VARCHAR)")
    con.execute("CREATE TABLE article_tags(article_id VARCHAR, source_type VARCHAR, category VARCHAR)")
    con.execute(
        "INSERT INTO clusters VALUES (?, ?, ?, ?, ?, 'active')",
        ["cfeed123", "Hospitals report ransomware disruption", first, latest, json.dumps(members)],
    )
    con.executemany(
        "INSERT INTO cluster_members VALUES (?, 'cfeed123')",
        [(m["url"],) for m in members],
    )
    con.executemany(
        "INSERT INTO article_tags VALUES (?, 'gal', 'cyber_attacks')",
        [(m["url"],) for m in members[:3]],
    )
    con.close()
    candidates = publisher.scan_candidates()
    assert len(candidates) == 1
    assert candidates[0]["topic_name"] == "Cybersecurity"
    assert candidates[0]["source_count"] == 4
    assert candidates[0]["member_tags"] == 3


def test_one_judged_member_can_assign_topic_to_multisource_cluster(social_modules):
    _, publisher, tmp_path = social_modules
    now = datetime.now(timezone.utc)
    latest = int(now.strftime("%Y%m%d%H%M%S"))
    first = int((now - timedelta(hours=1)).strftime("%Y%m%d%H%M%S"))
    members = [
        {"url": f"https://publisher{i}.test/a", "domain": f"publisher{i}.test",
         "title": "FDA announces a product recall", "crawled_at": latest}
        for i in range(1, 4)
    ]
    con = duckdb.connect(str(tmp_path / "gdelt.duckdb"))
    con.execute("CREATE TABLE clusters(cluster_id VARCHAR, title VARCHAR, first_seen BIGINT, latest_seen BIGINT, members_json VARCHAR, status VARCHAR)")
    con.execute("CREATE TABLE cluster_members(article_url VARCHAR, cluster_id VARCHAR)")
    con.execute("CREATE TABLE article_tags(article_id VARCHAR, source_type VARCHAR, category VARCHAR)")
    con.execute("INSERT INTO clusters VALUES (?, ?, ?, ?, ?, 'active')",
                ["cfda123", "FDA announces a product recall", first, latest, json.dumps(members)])
    con.executemany("INSERT INTO cluster_members VALUES (?, 'cfda123')",
                    [(m["url"],) for m in members])
    con.execute("INSERT INTO article_tags VALUES (?, 'gal', 'fda_agency')", [members[0]["url"]])
    con.close()
    candidates = publisher.scan_candidates()
    assert len(candidates) == 1
    assert candidates[0]["topic_name"] == "FDA"
    assert candidates[0]["source_count"] == 3
    assert candidates[0]["member_tags"] == 1


def test_calibration_requires_real_pilot_data(social_modules):
    _, publisher, _ = social_modules
    ready, report = publisher.calibrate()
    assert not ready
    assert report["reviewed"] == 0
    assert "50" in report["reason"]
