"""Deterministic tests for social card-title helpers (no LLM calls).

Run directly with the repo venv:  python tests/test_social_title.py
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

PASSED, FAILED = [], []


def check(name, cond):
    (PASSED if cond else FAILED).append(name)
    print(("PASS" if cond else "FAIL"), "-", name)


# --- chrome: trailing pipe is always stripped -------------------------------
check("iHeart pipe suffix",
      clean_member_title("NY Sees First Measles Death In 30+ Years | Talk Radio 98.3 WLAC")
      == "NY Sees First Measles Death In 30+ Years")
check("pipe news-radio suffix",
      clean_member_title("World Leaders React To GOP Midterm Losses | News Radio 610")
      == "World Leaders React To GOP Midterm Losses")
check("double pipe suffix",
      clean_member_title("Big Story | Section Desk | 96-1 SRS") == "Big Story")
check("non-latin pipe suffix",
      clean_member_title("Tres personas fallecidas en Navarra | El Diario Montañés")
      == "Tres personas fallecidas en Navarra")

# --- chrome: trailing dash only when it matches the member's own outlet -----
check("dash outlet match",
      clean_member_title("Kevin Smith threatened to crash car, Ruddock tells court - Jamaica Observer",
                         "Jamaica Observer", None)
      == "Kevin Smith threatened to crash car, Ruddock tells court")
check("dash domain match",
      clean_member_title("Fireworks time limit as UK households face fine - Kent Online",
                         None, "kentonline.co.uk")
      == "Fireworks time limit as UK households face fine")
check("dash preserved (club name, foreign outlet)",
      clean_member_title("Me ponen el Betis - Chelsea de ejemplo", "El Mundo", "elmundo.es")
      == "Me ponen el Betis - Chelsea de ejemplo")
check("dash preserved (no outlet given)",
      clean_member_title("Road chase and dangerous driving cost motorist AED230,000")
      == "Road chase and dangerous driving cost motorist AED230,000")
check("em-dash outlet match",
      clean_member_title("Something happened — Daily Telegraph", "Daily Telegraph", None)
      == "Something happened")

# --- title grounding --------------------------------------------------------
SRC = ("1. [ap] A child died of measles in New York, the state's first measles "
       "death in over 30 years. Officials stressed MMR vaccination.")
EXPL = "A child died of measles in New York, the state's first measles death in over 30 years."
check("grounding ok",
      _title_grounding("New York child dies of measles, first in 30 years", EXPL, SRC)[0])
check("grounding rejects too long",
      not _title_grounding("x" * (TITLE_LIMIT + 1), EXPL, SRC)[0])
check("grounding rejects banned word",
      not _title_grounding("Measles cases could rise", EXPL, SRC)[0])
check("grounding rejects number absent from evidence",
      not _title_grounding("First measles death in 50 years", EXPL, SRC)[0])
check("grounding rejects all-caps wire style",
      not _title_grounding("NEW YORK CHILD DIES OF MEASLES", EXPL, SRC)[0])
check("grounding rejects empty", not _title_grounding("", EXPL, SRC)[0])
check("shouty detector", _shouty("BREAKING NEWS EVERYWHERE") and not _shouty("Normal sentence case"))

# --- degraded path: fallback post carries no chrome -------------------------
fe = factual_explanation(clean_member_title("World Leaders React To GOP Midterm Losses | News Radio 610"))
check("fallback post has no outlet chrome", "News Radio" not in fe and "|" not in fe)
check("fallback post ends with period", fe.endswith("."))

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
sys.exit(1 if FAILED else 0)
