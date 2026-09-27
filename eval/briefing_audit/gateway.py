"""Gateway client for the eval suite (stdlib only — runs in the project venv).

Everything goes through the self-hosted LLM gateway with the budgeted
``gdelt-eval`` key ($3/30d hard cap server-side), so spend shows up per app in
Langfuse and can never run away. On top of that each run has its own
``max_usd`` stop, counted from BILLED tokens, not estimates.

Jev (alias ``jev``) is not chat-shaped: the user message carries JSON
{state, questions} and the reply content is TypeSafe's raw JSON.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

GATEWAY_URL = os.environ.get("GDELT_EVAL_GATEWAY_URL", "https://llm.snambiar.com/v1/chat/completions")
KEY_FILE = Path(__file__).resolve().parents[2] / "data" / ".gdelt_eval_gateway_key"
JEV_MODEL = "jev-1.13.0"       # pinned: thresholds are tuned against this version
WRITER_MODEL = "accounts/fireworks/models/gpt-oss-120b"  # = dashboard/briefing.py BRIEFING_MODEL
PRICE_PER_MTOK = {             # observed/published input prices; output is free for Jev
    "jev": (0.042, 0.0),
    "fireworks-kimi": (None, None),  # measured from gateway spend in the pilot, never assumed
    # The briefing writer (replay A/B). Gateway config prices; reasoning tokens bill as output.
    WRITER_MODEL: (0.15, 0.60),
}


class BudgetExceeded(RuntimeError):
    pass


class JudgeError(RuntimeError):
    pass


class Gateway:
    def __init__(self, max_usd: float, key: str | None = None):
        self.key = key or os.environ.get("GDELT_EVAL_GATEWAY_KEY") or KEY_FILE.read_text().strip()
        self.max_usd = max_usd
        self.spent = 0.0
        self.calls = 0
        self.tokens_in = 0
        self._lock = threading.Lock()

    def _post(self, body: dict, retries: int = 4, timeout: int = 90) -> dict:
        data = json.dumps(body).encode()
        for attempt in range(retries):
            req = urllib.request.Request(GATEWAY_URL, data=data, headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
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
        """-> TypeSafe answers dict. Raises JudgeError on anything unusable."""
        self._check_budget()
        body = {"model": "jev", "messages": [{"role": "user", "content": json.dumps(
            {"model": JEV_MODEL, "state": state, "questions": questions})}]}
        r = self._post(body)
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
