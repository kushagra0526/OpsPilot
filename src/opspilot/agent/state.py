"""LangGraph state. Values are plain JSON-friendly types so the SQLite checkpointer can store them."""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """Everything one session's graph run reads and writes."""

    session_id: str
    turn_id: str
    user_input: str
    operator_id: str
    intent: dict[str, Any]
    memories: list[dict[str, Any]]
    bundle: dict[str, Any]
    decision: dict[str, Any]
    plan_history: list[dict[str, Any]]
    risk_assessments: dict[str, dict[str, Any]]
    approvals: dict[str, dict[str, Any]]
    pending_approvals: list[dict[str, Any]]
    step_results: list[dict[str, Any]]
    iteration: int
    grounding_retry: bool
    validation_errors: list[str]
    final_response: str
    trace: list[dict[str, Any]]
    usage: dict[str, Any]
    session_usage: dict[str, Any]
    conversation: list[dict[str, str]]
    memories_saved: list[dict[str, Any]]
    needs_replan: bool
