"""LLM access: Groq primary, optional Gemini fallback, retries with backoff."""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from opspilot.config import get_settings

Tier = Literal["large", "small"]
M = TypeVar("M", bound=BaseModel)
MAX_ATTEMPTS = 4


class MissingAPIKeyError(RuntimeError):
    """Raised when no LLM provider is configured."""


class LLMError(RuntimeError):
    """Raised when every provider/attempt failed."""


class DailyLimitError(RuntimeError):
    """A provider's daily quota is exhausted. Deliberately not an LLMError: it must propagate."""


def _is_daily_limit(exc: Exception) -> bool:
    """Groq says 'tokens per day (TPD)'; Gemini says 'GenerateRequestsPerDayPerProject...'."""
    text = str(exc).lower().replace(" ", "").replace("_", "")
    daily = any(k in text for k in ("perday", "daily", "(tpd)", "(rpd)"))
    return daily and any(k in text for k in ("rate", "limit", "quota", "exhausted"))


class LLMClient(Protocol):
    """Minimal interface every LLM backend (live or fake) implements."""

    def complete(
        self, node: str, system: str, user: str, *, tier: Tier, temperature: float, json_mode: bool
    ) -> str:
        """Return the model's text for one request."""
        ...

    def drain_calls(self) -> list[dict[str, Any]]:
        """Return and clear per-call records (usage, retries) since the last drain."""
        ...

    def describe(self) -> dict[str, str]:
        """Provider/model info for the UI."""
        ...


class _CallLog:
    """Thread-safe buffer of call records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[dict[str, Any]] = []

    def add(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._calls.append(record)

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            calls, self._calls = self._calls, []
        return calls


def _is_retryable(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in {
        "RateLimitError",
        "InternalServerError",
        "APIConnectionError",
        "APITimeoutError",
        "ServiceUnavailable",
        "ResourceExhausted",
        "DeadlineExceeded",
    }:
        return True
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return isinstance(status, int) and (status == 429 or status >= 500)


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        return float(headers.get("retry-after")) if headers.get("retry-after") else None
    except (TypeError, ValueError):
        return None


class LiveLLM:
    """Groq via langchain-groq, with Gemini fallback when GOOGLE_API_KEY is set."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        settings = get_settings()
        if not settings.groq_api_key and not settings.google_api_key:
            raise MissingAPIKeyError("GROQ_API_KEY is not set")
        self.settings = settings
        self._sleep = sleep
        self._log = _CallLog()
        self._models: dict[tuple[str, str, float, bool], Any] = {}

    def describe(self) -> dict[str, str]:
        """Provider/model info for the UI."""
        s = self.settings
        providers = self._providers()
        if providers[0] == "gemini":
            return {"provider": "gemini", "large": s.gemini_model, "small": s.gemini_model}
        info = {"provider": "groq", "large": s.large_model, "small": s.small_model}
        if "gemini" in providers:
            info["fallback"] = f"gemini ({s.gemini_model})"
        return info

    def drain_calls(self) -> list[dict[str, Any]]:
        """Return and clear per-call records."""
        return self._log.drain()

    def _model(self, provider: str, tier: Tier, temperature: float, json_mode: bool) -> Any:
        key = (provider, tier, temperature, json_mode)
        if key not in self._models:
            if provider == "groq":
                from langchain_groq import ChatGroq

                name = self.settings.large_model if tier == "large" else self.settings.small_model
                # gpt-oss models reason before answering: enough for planning, minimal elsewhere.
                effort = "medium" if tier == "large" else "low"
                extra = {"reasoning_effort": effort} if name.startswith("openai/gpt-oss") else {}
                model: Any = ChatGroq(
                    model=name,
                    temperature=temperature,
                    max_retries=0,
                    api_key=self.settings.groq_api_key,
                    max_tokens=4096,
                    **extra,
                )
                if json_mode:
                    model = model.bind(response_format={"type": "json_object"})
            else:
                from langchain_google_genai import ChatGoogleGenerativeAI

                model = ChatGoogleGenerativeAI(
                    model=self.settings.gemini_model,
                    temperature=temperature,
                    max_retries=0,
                    google_api_key=self.settings.google_api_key,
                    **({"response_mime_type": "application/json"} if json_mode else {}),
                )
            self._models[key] = model
        return self._models[key]

    def _providers(self) -> list[str]:
        """Providers to try, in order. OPSPILOT_PROVIDER=groq|gemini pins one; auto = groq→gemini."""
        keyed = [
            p
            for p, k in (
                ("groq", self.settings.groq_api_key),
                ("gemini", self.settings.google_api_key),
            )
            if k
        ]
        pinned = self.settings.provider
        if pinned in ("groq", "gemini"):
            if pinned not in keyed:
                raise MissingAPIKeyError(f"OPSPILOT_PROVIDER={pinned} but its API key is not set")
            return [pinned]
        return keyed

    def complete(
        self, node: str, system: str, user: str, *, tier: Tier, temperature: float, json_mode: bool
    ) -> str:
        """Call the model with exponential backoff + jitter; fall back to Gemini on failure."""
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        errors: list[str] = []
        daily_limited: list[str] = []
        for provider in self._providers():
            retries = 0
            for attempt in range(MAX_ATTEMPTS):
                start = time.time()
                try:
                    resp = self._model(provider, tier, temperature, json_mode).invoke(messages)
                except Exception as exc:
                    errors.append(f"{provider}: {type(exc).__name__}: {exc}")
                    if _is_daily_limit(exc):
                        daily_limited.append(provider)
                        break
                    if not _is_retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                        break
                    retries += 1
                    delay = min(
                        90.0,
                        _retry_after(exc) or min(20.0, 1.5 * 2**attempt) + random.uniform(0, 1),
                    )
                    self._log.add(
                        {
                            "node": node,
                            "event": "retry",
                            "provider": provider,
                            "attempt": attempt + 1,
                            "delay": round(delay, 1),
                            "error": type(exc).__name__,
                        }
                    )
                    self._sleep(delay)
                    continue
                usage = getattr(resp, "usage_metadata", None) or {}
                self._log.add(
                    {
                        "node": node,
                        "event": "call",
                        "provider": provider,
                        "tier": tier,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "retries": retries,
                        "latency": round(time.time() - start, 2),
                    }
                )
                content = resp.content
                if isinstance(content, list):
                    content = "".join(
                        c.get("text", "") if isinstance(c, dict) else str(c) for c in content
                    )
                return str(content)
        if daily_limited:
            raise DailyLimitError(
                f"daily quota exhausted for {', '.join(daily_limited)}: {errors[-1]}"
            )
        raise LLMError("; ".join(errors[-3:]) or "no provider configured")


def extract_json(text: str) -> Any:
    """Strip code fences / prose and parse the first JSON object."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    return json.loads(cleaned[start : end + 1])


def complete_structured(
    llm: LLMClient,
    node: str,
    system: str,
    user: str,
    model: type[M],
    *,
    tier: Tier = "large",
    temperature: float = 0.0,
) -> M:
    """Call the LLM for JSON matching `model`; retry once with the validation error appended."""
    prompt = user
    last_error = ""
    for _ in range(2):
        text = llm.complete(
            node, system, prompt, tier=tier, temperature=temperature, json_mode=True
        )
        try:
            return model.model_validate(extract_json(text))
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)[:1500]
            prompt = (
                f"{user}\n\nYOUR PREVIOUS OUTPUT WAS INVALID:\n{last_error}\n"
                "Return ONLY a corrected JSON object matching the required schema."
            )
    raise LLMError(f"could not parse {model.__name__}: {last_error}")


_override: LLMClient | None = None


def set_llm(client: LLMClient | None) -> None:
    """Dependency-injection hook: tests install their offline double here."""
    global _override
    _override = client


def get_llm() -> LLMClient:
    """The live Groq client (Gemini fallback when configured); tests may inject a double."""
    if _override is not None:
        return _override
    return LiveLLM()
