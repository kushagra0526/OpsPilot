"""Settings, paths and business thresholds. Values are read from the environment at call time."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")

KNOWLEDGE_DIR = ROOT_DIR / "data" / "knowledge"
POLICY_DIR = KNOWLEDGE_DIR / "policies"
FAQ_PATH = KNOWLEDGE_DIR / "faq.json"
INVENTORY_CSV = KNOWLEDGE_DIR / "inventory_feed.csv"

DEFAULT_REFERENCE_DATE = "2026-09-27"

# Business numbers (refund limit, return windows, tier limits...) are NOT defined here: they are
# read from the `rules:` front matter of the policy files by `policy_rules()` below, so each
# number lives in one place, next to the policy text that states it.
CONFIDENCE_APPROVAL_THRESHOLD = 0.6

MAX_ITERATIONS = 3
POLICY_TOP_K = 6
FAQ_TOP_K = 3
MAX_POLICY_CHUNKS = 10
# Cosine similarity (1 - distance) below which knowledge-base hits count as irrelevant.
RELEVANCE_THRESHOLD = 0.20
MEMORY_LOAD_LIMIT = 10


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from environment variables."""

    reference_date: date
    runtime_dir: Path
    groq_api_key: str | None
    google_api_key: str | None
    large_model: str
    small_model: str
    gemini_model: str
    provider: str = "auto"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        """SQLite database with business data, audit log and memory."""
        return self.runtime_dir / "opspilot.db"

    @property
    def chroma_dir(self) -> Path:
        """Persistent Chroma directory."""
        return self.runtime_dir / "chroma"

    @property
    def checkpoint_path(self) -> Path:
        """LangGraph SQLite checkpointer database."""
        return self.runtime_dir / "checkpoints.db"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.split("#", 1)[0].strip()
    return value or default


def get_settings() -> Settings:
    """Build settings from the current environment."""
    runtime = Path(_env("OPSPILOT_RUNTIME_DIR") or ROOT_DIR / "data" / "runtime").resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    return Settings(
        reference_date=date.fromisoformat(_env("OPSPILOT_TODAY", DEFAULT_REFERENCE_DATE) or ""),
        runtime_dir=runtime,
        groq_api_key=_env("GROQ_API_KEY"),
        google_api_key=_env("GOOGLE_API_KEY"),
        large_model=_env("OPSPILOT_LARGE_MODEL", "openai/gpt-oss-120b") or "",
        small_model=_env("OPSPILOT_SMALL_MODEL", "openai/gpt-oss-20b") or "",
        gemini_model=_env("OPSPILOT_GEMINI_MODEL", "gemini-flash-latest") or "",
        provider=(_env("OPSPILOT_PROVIDER", "auto") or "auto").lower(),
    )


def today() -> date:
    """The fixed reference date used for all date logic."""
    return get_settings().reference_date


@dataclass(frozen=True)
class PolicyRules:
    """Enforced business numbers, read from the currently effective policies' front matter."""

    auto_refund_limit: float
    return_windows: dict[str, int | None]  # days per category; None = non-returnable
    goodwill_limits: dict[str, int]  # max goodwill coupon % per loyalty tier
    max_coupon_percent: int
    coupon_validity_days: int
    promise_days: int
    delay_threshold_days: int
    delay_coupon_percent: int
    sources: dict[str, str] = field(default_factory=dict)  # rule name -> doc_id it came from


def _front_matter(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1]) or {}


@lru_cache(maxsize=8)
def _load_rules(policy_dir: str, ref: date, stamp: tuple[float, ...]) -> PolicyRules:
    docs = [_front_matter(p) for p in sorted(Path(policy_dir).glob("*.md"))]
    effective = [d for d in docs if date.fromisoformat(str(d["effective_date"])) <= ref]
    superseded = {d["supersedes"] for d in effective if d.get("supersedes")}
    values: dict = {}
    sources: dict[str, str] = {}
    for doc in effective:
        if doc["doc_id"] in superseded:
            continue
        for key, value in (doc.get("rules") or {}).items():
            if key in values:
                raise ValueError(f"rule {key!r} defined by both {sources[key]} and {doc['doc_id']}")
            values[key], sources[key] = value, doc["doc_id"]
    missing = [f for f in PolicyRules.__dataclass_fields__ if f != "sources" and f not in values]
    if missing:
        raise ValueError(f"no effective policy defines rules {missing} as of {ref.isoformat()}")
    return PolicyRules(**values, sources=sources)


def policy_rules() -> PolicyRules:
    """Rules from current (effective, non-superseded) policies; reloaded when a file changes."""
    stamp = tuple(p.stat().st_mtime for p in sorted(POLICY_DIR.glob("*.md")))
    return _load_rules(str(POLICY_DIR), today(), stamp)
