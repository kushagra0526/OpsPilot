"""LLM wrapper: JSON extraction, structured retry, backoff on rate limits (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fakes import FakeLLM

from opspilot.agent.schemas import AgentDecision
from opspilot.llm import LiveLLM, LLMError, complete_structured, extract_json


class RateLimitError(Exception):
    """Mimics groq.RateLimitError by name."""


def test_extract_json_strips_fences() -> None:
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_structured_retries_once_with_error() -> None:
    good = '{"decision_type": "answer", "analysis": "x", "confidence": 0.5}'
    llm = FakeLLM({"plan": ["not json", good]})
    out = complete_structured(llm, "plan", "sys", "user", AgentDecision)
    assert out.decision_type == "answer"
    assert "YOUR PREVIOUS OUTPUT WAS INVALID" in llm.calls[1]["user"]


def test_structured_gives_up_after_two_failures() -> None:
    with pytest.raises(LLMError):
        complete_structured(FakeLLM({"plan": ["{}", "{}"]}), "plan", "s", "u", AgentDecision)


def test_live_llm_backs_off_on_rate_limit(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    sleeps: list[float] = []
    llm = LiveLLM(sleep=sleeps.append)
    attempts = {"n": 0}

    class Model:
        def invoke(self, messages):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimitError("429 too many requests")
            return SimpleNamespace(
                content="hello", usage_metadata={"input_tokens": 5, "output_tokens": 2}
            )

    monkeypatch.setattr(llm, "_model", lambda *a, **k: Model())
    text = llm.complete("plan", "s", "u", tier="large", temperature=0, json_mode=False)
    assert text == "hello" and attempts["n"] == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0] - 1, "exponential backoff"
    calls = llm.drain_calls()
    assert [c["event"] for c in calls] == ["retry", "retry", "call"]
    assert calls[-1]["retries"] == 2


def test_live_llm_non_retryable_raises(monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    llm = LiveLLM(sleep=lambda s: None)

    class Model:
        def invoke(self, messages):
            raise ValueError("bad request")

    monkeypatch.setattr(llm, "_model", lambda *a, **k: Model())
    with pytest.raises(LLMError):
        llm.complete("plan", "s", "u", tier="large", temperature=0, json_mode=False)


def test_daily_limit_detection() -> None:
    from opspilot.llm import _is_daily_limit

    groq = RateLimitError("Rate limit reached for model on tokens per day (TPD): Limit 200000")
    gemini = RateLimitError(
        "429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    )
    minute = RateLimitError("Rate limit reached on tokens per minute (TPM): Limit 8000")
    assert _is_daily_limit(groq) and _is_daily_limit(gemini) and not _is_daily_limit(minute)


def test_daily_limit_stops_without_retry(monkeypatch) -> None:
    from opspilot.llm import DailyLimitError

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    sleeps: list[float] = []
    llm = LiveLLM(sleep=sleeps.append)

    class Model:
        def invoke(self, messages):
            raise RateLimitError("Rate limit reached on tokens per day (TPD)")

    monkeypatch.setattr(llm, "_model", lambda *a, **k: Model())
    with pytest.raises(DailyLimitError):
        llm.complete("plan", "s", "u", tier="large", temperature=0, json_mode=False)
    assert sleeps == []
