from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipeline.sec_copy_quality import check_mirrors, check_observations, check_text
from pipeline.sec_explain import observations
from dashboard.sec_dates import format_date_context


def test_date_context_past_present_future_missing_and_timestamp():
    now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
    assert format_date_context("2026-09-18", now=now) == "Sep 18, 2026 · 1 day ago"
    assert format_date_context("2026-09-19", now=now) == "Sep 19, 2026 · today"
    assert format_date_context("2026-09-21", now=now) == "Sep 21, 2026 · in 2 days"
    assert format_date_context(None, now=now) == "not stated"
    assert format_date_context("20260918", now=now) == "Sep 18, 2026 · 1 day ago"
    assert "Sep 18, 2026, 8:30 AM UTC-04:00 · 1 day ago" == format_date_context(
        "2026-09-18T08:30:00-04:00", now=now)


def test_copy_checker_rejects_damage_and_unsupported_language():
    codes = {x.code for x in check_text("Revenue � rose. Buy now; it will grow.")}
    assert {"encoding", "recommendation", "forecast"} <= codes


def test_observation_contract_requires_basis():
    class Observation:
        text = "Revenue fell 2.0% year over year."
        kind = "growth"
        basis = ""
    assert "claim-basis" in {x.code for x in check_observations([Observation()])}


def test_sec_templates_pass_portable_copy_contract():
    root = Path(__file__).resolve().parent.parent
    template = (root / "dashboard/templates/sec_analysis.html").read_text(encoding="utf-8")
    issues = check_text(template, label="sec template")
    assert not issues, issues
    assert not check_mirrors(root)


def test_emitted_observations_pass_the_copy_and_basis_contract():
    snapshot = {"revenue": 120_000_000, "net_income": 12_000_000,
                "operating_income": 15_000_000, "fp": "Q2"}
    derived = {"revenue_yoy": 0.2, "operating_margin": 0.125,
               "operating_margin_yoy_pp": 0.03}
    assert not check_observations(observations(snapshot, derived, {}))
