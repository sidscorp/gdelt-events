import json
import urllib.error

import numpy as np
import pytest

from pipeline import pill_eval, pill_judge, pill_scorer


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def _valid_gateway_response(content: str = '[{"n":1,"verdict":"relevant","reason":"ok"}]'):
    return _Response({"choices": [{"message": {"content": content}}]})


def _clear_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("GDELT_PILL_JUDGE_GATEWAY_KEY", raising=False)
    monkeypatch.setattr(pill_eval, "KEY_PATH", tmp_path / "dedicated")
    monkeypatch.setattr(pill_eval, "LEGACY_KEY_PATH", tmp_path / "legacy")
    monkeypatch.setattr(pill_eval, "_legacy_key_warning_emitted", False)


def test_credential_precedence_and_legacy_fallback(monkeypatch, tmp_path):
    _clear_credentials(monkeypatch, tmp_path)
    pill_eval.LEGACY_KEY_PATH.write_text("legacy-secret")
    assert pill_eval._resolve_key() == ("legacy-secret", "legacy_file")

    pill_eval.KEY_PATH.write_text("dedicated-secret")
    assert pill_eval._resolve_key() == ("dedicated-secret", "dedicated_file")

    monkeypatch.setenv("GDELT_PILL_JUDGE_GATEWAY_KEY", "environment-secret")
    assert pill_eval._resolve_key() == ("environment-secret", "environment")


def test_missing_or_empty_credential_fails_without_exposing_values(monkeypatch, tmp_path):
    _clear_credentials(monkeypatch, tmp_path)
    pill_eval.KEY_PATH.write_text("   ")
    with pytest.raises(pill_eval.JudgeConfigurationError) as caught:
        pill_eval._get_key()
    assert "credential is not configured" in str(caught.value)


def test_configuration_failure_makes_no_network_attempt(monkeypatch, tmp_path):
    _clear_credentials(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        pill_eval.urllib.request, "urlopen",
        lambda *_args, **_kwargs: calls.append(True),
    )
    with pytest.raises(pill_eval.JudgeConfigurationError):
        pill_eval._judge_call("test")
    assert calls == []


def test_authentication_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(pill_eval, "_get_key", lambda: "secret")
    calls = []

    def reject(*_args, **_kwargs):
        calls.append(True)
        raise urllib.error.HTTPError("https://gateway.invalid", 401, "no", {}, None)

    monkeypatch.setattr(pill_eval.urllib.request, "urlopen", reject)
    monkeypatch.setattr(pill_eval.time, "sleep", lambda _seconds: pytest.fail("slept"))
    with pytest.raises(pill_eval.JudgeAuthenticationError):
        pill_eval._judge_call("test", retries=3)
    assert len(calls) == 1


def test_rate_limit_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(pill_eval, "_get_key", lambda: "secret")
    calls = []
    sleeps = []

    def open_request(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise urllib.error.HTTPError("https://gateway.invalid", 429, "slow", {}, None)
        return _valid_gateway_response()

    monkeypatch.setattr(pill_eval.urllib.request, "urlopen", open_request)
    monkeypatch.setattr(pill_eval.time, "sleep", sleeps.append)
    assert "relevant" in pill_eval._judge_call("test", retries=3)
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_invalid_gateway_json_is_a_response_error(monkeypatch):
    monkeypatch.setattr(pill_eval, "_get_key", lambda: "secret")

    class BadResponse(_Response):
        def read(self):
            return b"not-json"

    monkeypatch.setattr(
        pill_eval.urllib.request, "urlopen", lambda *_args, **_kwargs: BadResponse({}),
    )
    with pytest.raises(pill_eval.JudgeResponseError):
        pill_eval._judge_call("test")


def test_run_wide_breaker_stops_all_later_categories(monkeypatch):
    calls = []

    def fail(_prompt):
        calls.append(True)
        raise pill_eval.JudgeAuthenticationError("authentication failed")

    monkeypatch.setattr(pill_judge, "_judge_call", fail)
    breaker = pill_judge.JudgeCircuitBreaker()
    items = [{"url": "u1", "title": "A sufficiently long title", "desc": ""}]

    assert pill_judge.judge("ai_general", items, breaker=breaker) is None
    assert pill_judge.judge("cyber_attacks", items, breaker=breaker) is None
    assert len(calls) == 1
    assert breaker.opened
    assert breaker.failure_code == "authentication_failed"


def test_incomplete_batch_is_split_and_recovered(monkeypatch):
    # 16 verdicts for a batch of 20 is incomplete; since fc82bce the batch is
    # retried as two halves of 10, each fully covered by the same reply shape.
    content = json.dumps([
        {"n": i, "verdict": "relevant", "reason": "ok"}
        for i in range(1, 17)
    ])
    calls = []
    monkeypatch.setattr(pill_judge, "_judge_call", lambda _prompt: calls.append(1) or content)
    breaker = pill_judge.JudgeCircuitBreaker()
    items = [
        {"url": f"u{i}", "title": f"A sufficiently long article title {i}", "desc": ""}
        for i in range(20)
    ]
    out = pill_judge.judge("ai_general", items, breaker=breaker)
    assert out == {f"u{i}": "relevant" for i in range(20)}
    assert len(calls) == 3 and not breaker.opened


def test_incomplete_batch_too_small_to_split_opens_breaker(monkeypatch):
    content = json.dumps([{"n": 1, "verdict": "relevant", "reason": "ok"}])
    monkeypatch.setattr(pill_judge, "_judge_call", lambda _prompt: content)
    breaker = pill_judge.JudgeCircuitBreaker()
    items = [
        {"url": f"u{i}", "title": f"A sufficiently long article title {i}", "desc": ""}
        for i in range(2 * pill_judge.MIN_SPLIT - 1)
    ]
    assert pill_judge.judge("ai_general", items, breaker=breaker) is None
    assert breaker.failure_code == "invalid_response"


def _configure_scorer_test(monkeypatch, tmp_path, head: int = 11):
    watermark = tmp_path / "watermark"
    watermark.write_text("10\n")
    # The backlog guard compares the watermark to the embedding store head;
    # never let a unit test read the real store (~13M rows) through it.
    monkeypatch.setattr(pill_scorer.embedding_store, "total_rows", lambda: head)
    monkeypatch.setattr(pill_scorer, "WATERMARK", watermark)
    monkeypatch.setattr(pill_scorer, "JUDGE_HEALTH", tmp_path / "health.json")
    monkeypatch.setattr(
        pill_scorer,
        "load_pill_defs",
        lambda: [{"key": "ai_general", "kind": "curated"}],
    )
    return watermark


def test_scorer_preflight_failure_preserves_watermark_and_skips_expensive_work(
        monkeypatch, tmp_path):
    watermark = _configure_scorer_test(monkeypatch, tmp_path)

    def fail_preflight(breaker):
        breaker.trip(pill_eval.JudgeConfigurationError("credential missing"))
        return False

    monkeypatch.setattr(pill_judge, "preflight", fail_preflight)
    monkeypatch.setattr(
        pill_scorer, "get_pill_vectors",
        lambda _pills: pytest.fail("pill vectors loaded"),
    )
    monkeypatch.setattr(
        pill_scorer, "_open_read",
        lambda: pytest.fail("database opened"),
    )

    before = watermark.read_bytes()
    result = pill_scorer.score_new()
    assert watermark.read_bytes() == before
    assert result["failure_code"] == "credential_missing"
    assert result["articles"] == 0
    health = json.loads(pill_scorer.JUDGE_HEALTH.read_text())
    assert health["status"] == "failed"
    assert health["watermark_before"] == health["watermark_after"] == 10


def test_backlog_guard_skips_to_near_head_instead_of_paying(monkeypatch, tmp_path):
    head = 10 + pill_scorer.MAX_LAG_ROWS + 1
    watermark = _configure_scorer_test(monkeypatch, tmp_path, head=head)

    def fail_preflight(breaker):
        breaker.trip(pill_eval.JudgeConfigurationError("credential missing"))
        return False

    monkeypatch.setattr(pill_judge, "preflight", fail_preflight)
    pill_scorer.score_new()
    assert int(watermark.read_text()) == head - pill_scorer.SKIP_KEEP_ROWS


class _ReadConnection:
    def close(self):
        pass


def test_successful_bounded_run_advances_watermark(monkeypatch, tmp_path):
    watermark = _configure_scorer_test(monkeypatch, tmp_path)
    monkeypatch.setattr(pill_judge, "preflight", lambda _breaker: True)
    monkeypatch.setattr(pill_scorer, "get_pill_vectors", lambda _pills: {})
    monkeypatch.setattr(pill_scorer, "_open_read", _ReadConnection)
    monkeypatch.setattr(
        pill_scorer.embedding_store,
        "iter_active_chunks",
        lambda **_kwargs: iter([(["u1"], np.zeros((1, 1)), 10)]),
    )
    monkeypatch.setattr(
        pill_scorer,
        "stage_batch",
        lambda *_args, **_kwargs: ([], [], {"judged": 1}),
    )

    result = pill_scorer.score_new(chunk_rows=1, max_chunks=1)
    assert watermark.read_text() == "11"
    assert result["judged"] == 1
    assert result["bounded_run_complete"] is True
    assert json.loads(pill_scorer.JUDGE_HEALTH.read_text())["status"] == "ok"


def test_failed_chunk_holds_watermark(monkeypatch, tmp_path):
    watermark = _configure_scorer_test(monkeypatch, tmp_path)
    monkeypatch.setattr(pill_judge, "preflight", lambda _breaker: True)
    monkeypatch.setattr(pill_scorer, "get_pill_vectors", lambda _pills: {})
    monkeypatch.setattr(pill_scorer, "_open_read", _ReadConnection)
    monkeypatch.setattr(
        pill_scorer.embedding_store,
        "iter_active_chunks",
        lambda **_kwargs: iter([(["u1"], np.zeros((1, 1)), 10)]),
    )
    monkeypatch.setattr(
        pill_scorer,
        "stage_batch",
        lambda *_args, **_kwargs: (
            [], [], {"judged": 0, "judge_failed": True,
                     "failure_code": "provider_unavailable"}
        ),
    )

    before = watermark.read_bytes()
    result = pill_scorer.score_new(chunk_rows=1, max_chunks=1)
    assert watermark.read_bytes() == before
    assert result["halted_on_judge_failure"] is True
    assert result["failure_code"] == "provider_unavailable"
