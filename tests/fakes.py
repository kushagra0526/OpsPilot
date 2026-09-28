"""Helpers for building scripted FakeLLM plans in tests."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from opspilot.llm import LLMError, Tier, _CallLog
from opspilot.service import AgentService

ScriptItem = str | Callable[[str, str], str]


class FakeLLM:
    """Deterministic test double: scripted responses keyed by node name, consumed in order."""

    def __init__(
        self,
        scripts: dict[str, list[ScriptItem] | ScriptItem] | None = None,
        defaults: dict[str, ScriptItem] | None = None,
    ) -> None:
        self.scripts: dict[str, list[ScriptItem]] = {
            k: (v if isinstance(v, list) else [v]) for k, v in (scripts or {}).items()
        }
        self.defaults: dict[str, ScriptItem] = {
            "understand": lambda s, u: "{}",
            "respond": "",
            "memory": '{"memories": [], "interaction_summary": ""}',
            **(defaults or {}),
        }
        self.calls: list[dict[str, Any]] = []
        self._log = _CallLog()

    def describe(self) -> dict[str, str]:
        """Provider/model info for the UI."""
        return {"provider": "fake", "large": "scripted", "small": "scripted"}

    def drain_calls(self) -> list[dict[str, Any]]:
        """Return and clear per-call records."""
        return self._log.drain()

    def complete(
        self, node: str, system: str, user: str, *, tier: Tier, temperature: float, json_mode: bool
    ) -> str:
        """Pop the next scripted response for this node (or use its default)."""
        self.calls.append({"node": node, "system": system, "user": user, "tier": tier})
        queue = self.scripts.get(node)
        item = queue.pop(0) if queue else self.defaults.get(node)
        if item is None:
            raise LLMError(f"FakeLLM has no script for node {node!r}")
        text = item(system, user) if callable(item) else item
        self._log.add(
            {
                "node": node,
                "event": "call",
                "provider": "fake",
                "tier": tier,
                "input_tokens": len(system + user) // 4,
                "output_tokens": len(text) // 4,
                "retries": 0,
                "latency": 0.0,
            }
        )
        return text


def step(
    sid: str, tool: str, args: dict[str, Any], cites: list[str], depends_on: list[str] | None = None
) -> dict[str, Any]:
    """One plan step with a cited rationale."""
    return {
        "id": sid,
        "tool": tool,
        "args": args,
        "depends_on": depends_on or [],
        "rationale": f"{tool} because [{cites[0]}]",
        "citations": cites,
        "expected_outcome": f"{tool} succeeds",
    }


def plan(
    decision_type: str,
    analysis: str,
    steps: list[dict[str, Any]] | None = None,
    answer: str | None = None,
    confidence: float = 0.9,
) -> str:
    """AgentDecision JSON as the planner would return it."""
    return json.dumps(
        {
            "decision_type": decision_type,
            "analysis": analysis,
            "conflicts_considered": [],
            "missing_information": [],
            "steps": steps or [],
            "answer_draft": answer,
            "confidence": confidence,
        }
    )


def service(plans: list[Any], **extra: Any) -> tuple[AgentService, FakeLLM]:
    """AgentService backed by a FakeLLM with the given scripted plans."""
    llm = FakeLLM({"plan": list(plans), **extra})
    return AgentService(llm=llm), llm


def new_session() -> str:
    """Unique session id per test."""
    return f"test-{uuid.uuid4().hex[:8]}"


EMAIL = {"customer_id": "C-001", "subject": "Your refund", "body": "Your refund was processed."}
