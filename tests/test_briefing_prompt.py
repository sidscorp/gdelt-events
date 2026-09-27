"""The shipped writer prompt must be exactly what the replay A/B measured."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT))

import briefing  # noqa: E402
from eval.briefing_audit.replay import EDITS_V1  # noqa: E402

SOURCES = [
    {"n": 3, "title": "Agency weighs shutdown of visa scheme", "description": "Officials are considering it.",
     "outlet": "Reuters", "n_sources": 5, "link": "https://example.test/a", "url": "https://example.test/a"},
    {"n": 7, "title": "Company plans layoffs", "description": "The firm said it may cut jobs.",
     "outlet": "article.wn.com", "n_sources": 1, "link": "https://example.test/b", "url": "https://example.test/b"},
]


def test_writer_prompt_carries_the_measured_edits():
    prompt = briefing._build_briefing_prompt(SOURCES, "Global News", "", 3)
    if "local" in briefing.BRIEFING_MODEL:
        return  # the local-model template is a different, minimal format
    for _anchor, replacement in EDITS_V1:
        assert replacement in prompt
    assert "No hedging" not in prompt
