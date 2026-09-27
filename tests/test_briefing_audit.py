"""Offline tests for eval/briefing_audit (no network, no spend)."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval.briefing_audit import checks, judge, store  # noqa: E402
from eval.briefing_audit.gateway import BudgetExceeded, Gateway, JudgeError  # noqa: E402
from eval.briefing_audit.parse import parse_briefing, parse_sources  # noqa: E402

PROMPT = """Numbered sources:
3. [NEW × Reuters × 5 sources] Agency weighs shutdown of visa scheme — Officials are considering ending the programme after a fraud probe.
   (selected: big)
7. [NEW × article.wn.com] Company plans 500 layoffs — The firm said it may cut 500 jobs next year.
"""
SOURCES = json.dumps([
    {"n": 3, "title": "Agency weighs shutdown of visa scheme", "outlet": "Reuters", "n_sources": 5, "link": "https://x/e/1", "chosen": True},
    {"n": 7, "title": "Company plans 500 layoffs", "outlet": "article.wn.com", "n_sources": 1, "link": "https://article.wn.com/view/2", "chosen": True},
    {"n": 9, "title": "Unchosen story", "outlet": "AP", "n_sources": 2, "link": "https://x/e/9", "chosen": False},
])
BRIEFING = """## Global News — Last 3 Hours

**Executive Summary** – The agency has shut down the visa scheme. The U.S. move follows a fraud probe.

**Key Highlights**

- **Layoffs** – The company cut 800 jobs on Monday.  It could reshape the sector[7].
- **Visas** – Officials are weighing an end to the programme[3].
- **Bad cite** – Something happened here today[9][12].

**What to watch:** Whether the agency acts.
"""


def test_parse_units_sections_and_inherited_cites():
    units = parse_briefing(BRIEFING)
    secs = [u.section for u in units]
    assert secs[:2] == ["summary", "summary"]
    assert units[0].text.startswith("The agency has shut down")
    assert "U.S. move" in units[1].text  # abbreviation did not split the sentence
    lay = [u for u in units if "cut 800" in u.text][0]
    assert lay.cites == [7] and lay.section == "highlight"  # inherits the bullet's end citation
    assert units[-1].section == "watch"


def test_markdown_headings_switch_section_not_units():
    units = parse_briefing("The lede sentence is here.\n\n### Key highlights\n\n- **X** – A highlight sentence here[3].")
    assert [u.section for u in units] == ["summary", "highlight"]
    assert not any("Key highlights" in u.text for u in units)


def test_sources_recover_descriptions_from_prompt():
    s = parse_sources(SOURCES, PROMPT)
    assert s[3].description.startswith("Officials are considering")
    assert s[7].outlet == "article.wn.com" and not s[9].chosen


def test_free_checks():
    s = parse_sources(SOURCES, PROMPT)
    units = {u.text[:12]: u for u in parse_briefing(BRIEFING)}
    lead = parse_briefing(BRIEFING)[0]
    assert "uncited_summary" in checks.check_unit(lead, s)
    lay = [u for u in parse_briefing(BRIEFING) if "cut 800" in u.text][0]
    f = checks.check_unit(lay, s)
    assert any(x.startswith("number_not_in_sources:800") for x in f)   # source says 500
    assert "possible_escalation" in f                                   # "may cut" -> "cut"
    bad = [u for u in parse_briefing(BRIEFING) if "Something happened" in u.text][0]
    f = checks.check_unit(bad, s)
    assert "cite_not_chosen:9" in f and "cite_missing:12" in f
    lc = checks.lead_check(parse_briefing(BRIEFING), s)
    assert lc["lead_cited"] is False and lc["lead_source"] == 3


def test_lead_aggregator_single_source():
    s = parse_sources(SOURCES, PROMPT)
    text = "**Executive Summary** – The company plans layoffs of 500 people[7]."
    lc = checks.lead_check(parse_briefing(text), s)
    assert lc["lead_single_source"] and lc["lead_aggregator"]


class FakeGW(Gateway):
    def __init__(self, answers=None, fail=False, max_usd=1.0):
        self.max_usd, self.spent, self.calls, self.tokens_in = max_usd, 0.0, 0, 0
        import threading; self._lock = threading.Lock()
        self.answers, self.fail = answers, fail

    def jev(self, state, questions):
        self._check_budget()
        self.calls += 1
        self.spent += 0.4
        if self.fail:
            raise JudgeError("jev returned no answers")
        return self.answers


def test_judge_maps_answers_and_escalates():
    s = parse_sources(SOURCES, PROMPT)
    units = parse_briefing(BRIEFING)
    ans = {"verdict": {"choice": "overstated", "confidence": 0.9, "probabilities": {"overstated": 0.9}},
           "escalates": {"noul": 0.8}, "adds_specifics": {"noul": 0.1}}
    res = judge.judge_units(FakeGW(ans, max_usd=100), units, s, {})
    assert res[0]["verdict"] == "overstated" and res[0]["escalate"] is True


def test_judge_error_is_recorded_not_guessed():
    s = parse_sources(SOURCES, PROMPT)
    res = judge.judge_units(FakeGW(fail=True, max_usd=100), parse_briefing(BRIEFING), s, {})
    assert all(v["judged"] is False and v["verdict"] is None for v in res.values())


def test_budget_stop_raises():
    s = parse_sources(SOURCES, PROMPT)
    ans = {"verdict": {"choice": "supported", "confidence": 0.9}, "escalates": {"noul": 0.1}, "adds_specifics": {"noul": 0.1}}
    with pytest.raises(BudgetExceeded):
        judge.judge_units(FakeGW(ans, max_usd=0.5), parse_briefing(BRIEFING), s, {}, workers=1)


def test_replay_edits_apply_exactly_or_skip():
    from eval.briefing_audit.replay import EDITS_V1, apply_edits
    current = "…not a survey. Say why. CITATIONS: After each highlight (and any specific factual claim), cite x. " \
              "Be specific and concrete. No filler. No hedging. Use markdown."
    out = apply_edits(current, EDITS_V1)
    assert out and "No hedging" not in out and "KEEP EACH STORY'S CERTAINTY" in out
    assert "each factual sentence of the executive summary" in out
    assert apply_edits(current.replace("No hedging. ", ""), EDITS_V1) is None   # older prompt: skip, don't half-edit


def test_users_db_is_read_only(tmp_path):
    p = tmp_path / "users.db"
    sqlite3.connect(p).execute("CREATE TABLE briefing_history (id INTEGER)").connection.commit()
    con = store.users_db_ro(p)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO briefing_history VALUES (1)")


def test_real_h1b_briefing_if_available():
    export = Path(__file__).resolve().parents[1] / "data" / "bh_export.jsonl"
    if not export.exists():
        pytest.skip("no local export")
    r = next(json.loads(l) for l in export.open() if json.loads(l)["id"] == 4041)
    s = parse_sources(r["sources_json"], json.loads(r["meta_json"])["prompt"])
    units = parse_briefing(r["briefing"])
    lc = checks.lead_check(units, s)
    assert units[0].section == "summary" and "shut down the H-1B" in units[0].text
    assert "uncited_summary" in checks.check_unit(units[0], s)
    assert lc["lead_source"] == 37 and lc["lead_single_source"] and lc["lead_aggregator"]
