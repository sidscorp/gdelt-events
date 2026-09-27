"""Focused contracts for story-thread model calls."""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT))

import briefing  # noqa: E402


class _Connection:
    def __init__(self):
        self.executed = []
        self.committed = False
        self.closed = False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_thread_update_uses_structured_reasoning_headroom(monkeypatch):
    calls = []
    connection = _Connection()

    def fake_chat(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return json.dumps([
            {
                "slug": "continuing-story",
                "title": "Continuing story",
                "first_seen": "2026-09-26",
                "last_update": "2026-09-27",
                "summary": "A verified development occurred.",
                "status": "active",
            }
        ]), {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    monkeypatch.setattr(briefing, "_get_openrouter_key", lambda: "test-key")
    monkeypatch.setattr(briefing, "_chat", fake_chat)
    monkeypatch.setattr(briefing, "_get_langfuse", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "models",
        types.SimpleNamespace(get_user_db=lambda: connection),
    )

    briefing._update_threads("global:3", [], "A sourced update [1].")

    assert len(calls) == 1
    assert calls[0][1] == {
        "max_tokens": briefing.EDITOR_MAX_TOKENS,
        "temperature": 0.1,
        "reasoning_effort": "low",
    }
    assert connection.committed
    assert connection.closed
    _sql, params = connection.executed[0]
    assert params[0] == "global"
    assert json.loads(params[1])[0]["slug"] == "continuing-story"
