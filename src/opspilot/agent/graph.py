"""StateGraph wiring with a SQLite checkpointer (thread_id = session_id)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from opspilot.agent.nodes import AgentNodes, runnable_steps
from opspilot.agent.state import AgentState
from opspilot.llm import LLMClient

RECURSION_LIMIT = 60


def route_after_validate(state: AgentState) -> str:
    """Retry grounding, answer directly, go to the approval gate, or observe a fully blocked plan."""
    if state.get("grounding_retry"):
        return "reason_and_plan"
    steps = (state.get("decision") or {}).get("steps") or []
    if not steps:
        return "respond"
    return "approval_gate" if runnable_steps(state) else "observe"


def route_after_gate(state: AgentState) -> str:
    """Interrupt for approval only when something is pending."""
    return "await_approval" if state.get("pending_approvals") else "execute"


def route_after_observe(state: AgentState) -> str:
    """Replan while there is something to fix and iterations remain."""
    return "reason_and_plan" if state.get("needs_replan") else "respond"


def build_graph(llm: LLMClient, checkpointer: Any) -> Any:
    """Compile the agent graph."""
    n = AgentNodes(llm)
    g = StateGraph(AgentState)
    for name in (
        "understand",
        "load_memory",
        "retrieve",
        "reason_and_plan",
        "validate",
        "approval_gate",
        "await_approval",
        "execute",
        "observe",
        "respond",
        "save_memory",
    ):
        g.add_node(name, getattr(n, name))
    g.add_edge(START, "understand")
    g.add_edge("understand", "load_memory")
    g.add_edge("load_memory", "retrieve")
    g.add_edge("retrieve", "reason_and_plan")
    g.add_edge("reason_and_plan", "validate")
    g.add_conditional_edges(
        "validate", route_after_validate, ["reason_and_plan", "respond", "approval_gate", "observe"]
    )
    g.add_conditional_edges("approval_gate", route_after_gate, ["await_approval", "execute"])
    g.add_edge("await_approval", "execute")
    g.add_edge("execute", "observe")
    g.add_conditional_edges("observe", route_after_observe, ["reason_and_plan", "respond"])
    g.add_edge("respond", "save_memory")
    g.add_edge("save_memory", END)
    return g.compile(checkpointer=checkpointer)


def make_checkpointer(path: Path) -> SqliteSaver:
    """SQLite checkpointer shared across threads (SqliteSaver serialises access internally)."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn)
