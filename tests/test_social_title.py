"""Deterministic tests for social card-title helpers (no LLM calls).

Runs under pytest, or directly with the repo venv:
    python tests/test_social_title.py
Chrome fixtures come from a real gal_recent sample (2026-11-03).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

from social_publisher import (  # noqa: E402
    TITLE_LIMIT, _shouty, _title_grounding, clean_member_title,
    factual_explanation,
)

SRC = ("1. [ap] A child died of measles in New York, the state's first measles "
       "death in over 30 years. Officials stressed MMR vaccination.")
EXPL = "A child died of measles in New York, the state's first measles death in over 30 years."


# --- chrome: trailing pipe is always stripped -------------------------------
def test_iheart_pipe_suffix():
    assert (clean_member_title("NY Sees First Measles Death In 30+ Years | Talk Radio 98.3 WLAC")
            == "NY Sees First Measles Death In 30+ Years")


def test_pipe_news_radio_suffix():
    assert (clean_member_title("World Leaders React To GOP Midterm Losses | News Radio 610")
            == "World Leaders React To GOP Midterm Losses")


def test_double_pipe_suffix():
    assert clean_member_title("Big Story | Section Desk | 96-1 SRS") == "Big Story"


def test_non_latin_pipe_suffix():
    assert (clean_member_title("Tres personas fallecidas en Navarra | El Diario Montañés")
            == "Tres personas fallecidas en Navarra")


# --- chrome: trailing dash only when it matches the member's own outlet -----
def test_dash_outlet_match():
    assert (clean_member_title(
                "Kevin Smith threatened to crash car, Ruddock tells court - Jamaica Observer",
                "Jamaica Observer", None)
            == "Kevin Smith threatened to crash car, Ruddock tells court")


def test_dash_domain_match():
    assert (clean_member_title("Fireworks time limit as UK households face fine - Kent Online",
                               None, "kentonline.co.uk")
            == "Fireworks time limit as UK households face fine")


def test_dash_preserved_for_foreign_outlet():
    assert (clean_member_title("Me ponen el Betis - Chelsea de ejemplo", "El Mundo", "elmundo.es")
            == "Me ponen el Betis - Chelsea de ejemplo")


def test_dash_preserved_without_outlet():
    assert (clean_member_title("Road chase and dangerous driving cost motorist AED230,000")
            == "Road chase and dangerous driving cost motorist AED230,000")


def test_em_dash_outlet_match():
    assert (clean_member_title("Something happened — Daily Telegraph", "Daily Telegraph", None)
            == "Something happened")


# --- title grounding --------------------------------------------------------
def test_grounding_ok():
    assert _title_grounding("New York child dies of measles, first in 30 years", EXPL, SRC)[0]


def test_grounding_rejects_too_long():
    assert not _title_grounding("x" * (TITLE_LIMIT + 1), EXPL, SRC)[0]


def test_grounding_rejects_banned_word():
    assert not _title_grounding("Measles cases could rise", EXPL, SRC)[0]


def test_grounding_rejects_number_absent_from_evidence():
    assert not _title_grounding("First measles death in 50 years", EXPL, SRC)[0]


def test_grounding_rejects_all_caps_wire_style():
    assert not _title_grounding("NEW YORK CHILD DIES OF MEASLES", EXPL, SRC)[0]


def test_grounding_rejects_empty():
    assert not _title_grounding("", EXPL, SRC)[0]


def test_shouty_detector():
    assert _shouty("BREAKING NEWS EVERYWHERE") and not _shouty("Normal sentence case")


# --- degraded path: fallback post carries no chrome -------------------------
def test_fallback_post_has_no_outlet_chrome():
    fe = factual_explanation(
        clean_member_title("World Leaders React To GOP Midterm Losses | News Radio 610"))
    assert "News Radio" not in fe and "|" not in fe


def test_fallback_post_ends_with_period():
    fe = factual_explanation(
        clean_member_title("World Leaders React To GOP Midterm Losses | News Radio 610"))
    assert fe.endswith(".")


if __name__ == "__main__":
    import traceback

    tests = [obj for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = []
    for test in tests:
        try:
            test()
        except AssertionError:
            failed.append(test.__name__)
            print("FAIL -", test.__name__)
            traceback.print_exc()
        else:
            print("PASS -", test.__name__)
    print(f"\n{len(tests) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)
