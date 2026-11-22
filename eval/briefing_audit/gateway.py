"""Gateway client for the eval suite (stdlib only — runs in the project venv).

Everything goes through the self-hosted LLM gateway with the budgeted
``gdelt-eval`` key ($3/30d hard cap server-side), so spend shows up per app in
Langfuse and can never run away. On top of that each run has its own
``max_usd`` stop, counted from BILLED tokens, not estimates.

The judge (alias ``jev``, which is Venice Clef since 2026-10-22) is not
chat-shaped: the user message carries JSON {state, questions} and the reply
content is the provider's raw JSON.

RATE LIMITING (added 2026-11-12). Venice's /v1/systemone allows only ~50 requests
per short window, and the judge fires one call per briefing sentence — 150-250 per
briefing, fanned out. Before this, a run burned the whole window in seconds and
every call 429'd: 0 successes across 10 days, ~700-1,600 failed attempts/day, so
the audit silently produced nothing. Two layers now:
  * a local sliding-window limiter (default 45 calls / 120 s, env-tunable), and
  * server feedback: the x-ratelimit-remaining / x-ratelimit-resets headers park
    the limiter until the window actually resets before retrying a 429.
Pacing is per Gateway instance, so one audit run paces itself; two simultaneous
runs still share the server-side window and will back off rather than fail.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

GATEWAY_URL_FILE = Path(__file__).resolve().parents[2] / "data" / ".gateway_url"


def _gateway_url() -> str:
    """LLM gateway endpoint, configured on the host.

    Resolution: GDELT_EVAL_GATEWAY_URL, then data/.gateway_url. Never a literal
    in the repo — this repository is public and the endpoint is internal
    infrastructure.
    """
    url = os.environ.get("GDELT_EVAL_GATEWAY_URL") or (
        GATEWAY_URL_FILE.read_text(encoding="utf-8").strip()
        if GATEWAY_URL_FILE.exists() else None
    )
    if not url:
        raise RuntimeError(
            "no LLM gateway configured: set GDELT_EVAL_GATEWAY_URL or write "
            f"{GATEWAY_URL_FILE}"
        )
    return url


KEY_FILE = Path(__file__).resolve().parents[2] / "data" / ".gdelt_eval_gateway_key"
JEV_MODEL = "jev-1.13.0"       # sent inside the user JSON for the record; the deployment's model wins
WRITER_MODEL = "accounts/fireworks/models/gpt-oss-120b"  # = dashboard/briefing.py BRIEFING_MODEL
PRICE_PER_MTOK = {             # observed/published prices, $/1M tokens
    # Jev was output-free; Venice Clef (what `jev` actually resolves to since 2026-10-22)
    # bills $0.05/$0.05. Counting output keeps the max_usd stop honest.
    "jev": (0.05, 0.05),
    "fireworks-kimi": (None, None),  # measured from gateway spend in the pilot, never assumed
    # The briefing writer (replay A/B). Gateway config prices; reasoning tokens bill as output.
    WRITER_MODEL: (0.15, 0.60),
}

# Judge pacing. 45 (not 50) leaves headroom for another app sharing the same Venice key.
JUDGE_MAX_PER_WINDOW = int(os.environ.get("GDELT_EVAL_JUDGE_MAX_PER_WINDOW", "45"))
JUDGE_WINDOW_SEC = float(os.environ.get("GDELT_EVAL_JUDGE_WINDOW_SEC", "120"))


class BudgetExceeded(RuntimeError):
    pass


class JudgeError(RuntimeError):
    pass


class RateLimiter:
    """Thread-safe sliding-window limiter that also honours the server's own headers."""

    def __init__(self, max_calls: int = JUDGE_MAX_PER_WINDOW, window_sec: float = JUDGE_WINDOW_SEC):
        self.max_calls = max_calls
        self.window = window_sec
        self._times: deque[float] = deque()
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self.waits = 0
        self.waited_sec = 0.0

    def acquire(self) -> None:
        """Block until a request slot is free, then take it."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.window:
                    self._times.popleft()
                if self._blocked_until > now:
                    wait = self._blocked_until - now
                elif len(self._times) >= self.max_calls:
                    wait = self.window - (now - self._times[0]) + 0.05
                else:
                    self._times.append(now)
                    return
            # Sleep in slices so a second thread's release is picked up promptly.
            self.waits += 1
            slice_s = max(0.05, min(wait, 15.0))
            time.sleep(slice_s)
            self.waited_sec += slice_s

    def block_for(self, seconds: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + max(1.0, seconds))

    def note_headers(self, headers) -> None:
        """Park until the server's window resets when we are at the end of our quota."""
        if headers is None:
            return
        rem = headers.get("x-ratelimit-remaining")
        if rem is None:
            return
        try:
            remaining = int(rem)
        except (TypeError, ValueError):
            return
        if remaining > 2:
            return
        self.block_for(reset_delay(headers, default=60.0))


def reset_delay(headers, default: float = 30.0) -> float:
    """Seconds until the provider's rate-limit window resets, from x-ratelimit-resets."""
    if headers is None:
        return default
    raw = headers.get("x-ratelimit-resets")
    if not raw:
        return default
    try:
        epoch = float(raw)
    except (TypeError, ValueError):
        return default
    # Venice sends milliseconds since epoch; tolerate seconds just in case.
    if epoch > 1e11:
        epoch /= 1000.0
    return min(max(1.0, epoch - time.time() + 1.0), 600.0)


class Gateway:
    def __init__(self, max_usd: float, key: str | None = None):
        self.key = key or os.environ.get("GDELT_EVAL_GATEWAY_KEY") or KEY_FILE.read_text().strip()
        self.max_usd = max_usd
        self.spent = 0.0
        self.calls = 0
        self.tokens_in = 0
        self._lock = threading.Lock()
        self.judge_limiter = RateLimiter()

    def _post(self, body: dict, retries: int = 4, timeout: int = 90, limiter: RateLimiter | None = None) -> dict:
        data = json.dumps(body).encode()
        for attempt in range(retries):
            req = urllib.request.Request(_gateway_url(), data=data, headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    if limiter is not None:
                        limiter.note_headers(r.headers)
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code == 429 and limiter is not None and attempt + 1 < retries:
                    # Do NOT burn the retry budget on a short sleep: wait out the window.
                    limiter.block_for(reset_delay(e.headers, default=60.0))
                    limiter.acquire()
                    continue
                if e.code in (429, 500, 502, 503, 529) and attempt + 1 < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise JudgeError(f"gateway HTTP {e.code}: {e.read()[:200]!r}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt + 1 < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise JudgeError(f"gateway unreachable: {e}") from e
        raise JudgeError("gateway retries exhausted")

    def remaining_budget(self) -> float | None:
        """max_budget - spend for this key per the gateway; None if unknown
        (no cap, or the gateway is unreachable — callers then run normally,
        and the server-side cap still holds)."""
        url = _gateway_url().split("/v1/")[0] + "/key/info"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                info = json.loads(r.read().decode("utf-8", "replace")).get("info") or {}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        if info.get("max_budget") is None:
            return None
        return float(info["max_budget"]) - float(info.get("spend") or 0)

    def _charge(self, model: str, usage: dict):
        pin, pout = PRICE_PER_MTOK.get(model, (None, None))
        tin = int(usage.get("prompt_tokens") or 0)
        cost = (tin * (pin or 0) + int(usage.get("completion_tokens") or 0) * (pout or 0)) / 1e6
        with self._lock:
            self.calls += 1
            self.tokens_in += tin
            self.spent += cost

    def _check_budget(self):
        if self.spent >= self.max_usd:
            raise BudgetExceeded(f"run budget reached: ${self.spent:.4f} >= ${self.max_usd}")

    def jev(self, state, questions: dict) -> dict:
        """-> provider answers dict. Raises JudgeError on anything unusable."""
        self._check_budget()
        self.judge_limiter.acquire()
        body = {"model": "jev", "messages": [{"role": "user", "content": json.dumps(
            {"model": JEV_MODEL, "state": state, "questions": questions})}]}
        r = self._post(body, retries=6, limiter=self.judge_limiter)
        self._charge("jev", r.get("usage") or {})
        try:
            answers = json.loads(r["choices"][0]["message"]["content"])["answers"]
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise JudgeError(f"jev returned no answers: {str(r)[:200]}") from e
        if not answers:
            raise JudgeError("jev returned an empty answer set")
        return answers

    def write(self, prompt: str) -> str:
        """Re-run the briefing writer with the exact call shape of
        dashboard/briefing.py (one user message, max_tokens 8000, temp 0.3).
        A reasoning model can spend its budget thinking and return truncated
        or empty text; that is an error, never a briefing."""
        self._check_budget()
        r = self._post({"model": WRITER_MODEL, "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 8000, "temperature": 0.3}, retries=2, timeout=240)
        self._charge(WRITER_MODEL, r.get("usage") or {})
        try:
            choice = r["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise JudgeError(f"writer returned no choices: {str(r)[:200]}") from e
        if choice.get("finish_reason") == "length":
            raise JudgeError("writer truncated (finish_reason=length)")
        if len(text.strip()) < 200:
            raise JudgeError(f"writer returned {len(text.strip())} chars")
        return text
