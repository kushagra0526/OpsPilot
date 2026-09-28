"""AgentService: the single code path used by the Streamlit UI, the CLI and the evals."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from langgraph.types import Command

from opspilot import audit, db, memory
from opspilot.agent.citations import cited_ids, evidence_view
from opspilot.agent.graph import RECURSION_LIMIT, build_graph, make_checkpointer
from opspilot.agent.schemas import ApprovalDecision, RetrievalBundle, TurnOutcome
from opspilot.config import get_settings
from opspilot.llm import LLMClient, get_llm

# What each graph node is doing, phrased for an operator watching the turn run.
STEP_MESSAGES: dict[str, str] = {
    "understand": "Reading the request and extracting order, customer and product IDs",
    "load_memory": "Loading long-term memory for the customers involved",
    "retrieve": "Retrieving policies, order data, inventory and FAQ",
    "reason_and_plan": "Planning: reasoning over the retrieved evidence",
    "validate": "Checking citations, business rules and risk",
    "approval_gate": "Deciding which steps need human approval",
    "await_approval": "Recording approval decisions",
    "execute": "Executing steps",
    "observe": "Checking step results",
    "respond": "Writing the explanation",
    "save_memory": "Saving long-term memory",
}
ProgressCallback = Callable[[str, str], None]


class PendingApprovalError(RuntimeError):
    """A new turn was started while the session is waiting for approval decisions."""


class AgentService:
    """Start turns, resume after approvals, read audit/memories."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        settings = get_settings()
        self.llm = llm or get_llm()
        with db.transaction() as conn:
            db.init_schema(conn)
        self._checkpointer = make_checkpointer(settings.checkpoint_path)
        self.graph = build_graph(self.llm, self._checkpointer)

    @staticmethod
    def _config(session_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": RECURSION_LIMIT}

    def pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        """Steps waiting for a human decision in this session (empty if none)."""
        snapshot = self.graph.get_state(self._config(session_id))
        for intr in getattr(snapshot, "interrupts", ()) or ():
            return list(intr.value.get("pending", []))
        return []

    def _run(self, graph_input: Any, session_id: str, on_progress: ProgressCallback | None) -> None:
        """Run the graph; when a callback is given, report each node as it starts."""
        config = self._config(session_id)
        if on_progress is None:
            self.graph.invoke(graph_input, config)
            return
        for event in self.graph.stream(graph_input, config, stream_mode="tasks"):
            if "input" in event:  # task-start event (results carry "result" instead)
                on_progress(event["name"], STEP_MESSAGES.get(event["name"], event["name"]))

    def start_turn(
        self,
        session_id: str,
        user_input: str,
        operator_id: str = "operator-1",
        on_progress: ProgressCallback | None = None,
    ) -> TurnOutcome:
        """Run one operator request through the graph (may stop at an approval interrupt)."""
        if self.pending_approvals(session_id):
            raise PendingApprovalError(
                "This session is waiting for approval decisions; submit them first."
            )
        turn_id = uuid.uuid4().hex[:8]
        init = {
            "session_id": session_id,
            "turn_id": turn_id,
            "user_input": user_input.strip(),
            "operator_id": operator_id,
            "intent": {},
            "memories": [],
            "bundle": {},
            "decision": {},
            "plan_history": [],
            "risk_assessments": {},
            "approvals": {},
            "pending_approvals": [],
            "step_results": [],
            "iteration": 0,
            "grounding_retry": False,
            "validation_errors": [],
            "final_response": "",
            "trace": [],
            "usage": {},
            "memories_saved": [],
            "needs_replan": False,
        }
        self._run(init, session_id, on_progress)
        return self._save_turn(self._outcome(session_id), user_input.strip())

    def resume_with_approval(
        self,
        session_id: str,
        decisions: dict[str, ApprovalDecision | dict[str, Any]],
        on_progress: ProgressCallback | None = None,
    ) -> TurnOutcome:
        """Resume an interrupted turn with per-step approve/reject decisions."""
        if not self.pending_approvals(session_id):
            raise ValueError(f"session {session_id} has nothing awaiting approval")
        payload = {
            sid: (
                d if isinstance(d, ApprovalDecision) else ApprovalDecision.model_validate(d)
            ).model_dump()
            for sid, d in decisions.items()
        }
        self._run(Command(resume=payload), session_id, on_progress)
        return self._save_turn(self._outcome(session_id))

    def _outcome(self, session_id: str) -> TurnOutcome:
        snapshot = self.graph.get_state(self._config(session_id))
        state = snapshot.values
        pending = self.pending_approvals(session_id)
        bundle = RetrievalBundle.model_validate(state.get("bundle") or {})
        history = state.get("plan_history") or []
        ids = cited_ids([h["decision"] for h in history], bundle)
        used = [bundle.get(i) for i in ids] + [
            e for e in bundle.evidence if e.superseded and e.id not in ids
        ]
        turn_id = state.get("turn_id", "")
        return TurnOutcome(
            session_id=session_id,
            turn_id=turn_id,
            status="awaiting_approval" if pending else "completed",
            final_response=None if pending else state.get("final_response"),
            decision=state.get("decision") or None,
            plan_history=history,
            pending_approvals=pending,
            risk_assessments=state.get("risk_assessments") or {},
            step_results=state.get("step_results") or [],
            trace=[t for t in state.get("trace") or [] if t.get("turn_id") == turn_id],
            conflicts=[c.model_dump() for c in bundle.conflicts],
            missing=bundle.missing,
            evidence_used=[evidence_view(e, bundle) for e in used if e is not None],
            usage={
                "turn": state.get("usage") or {},
                "session": state.get("session_usage") or {},
                "model": self.llm.describe(),
            },
        )

    @staticmethod
    def _save_turn(out: TurnOutcome, user_input: str | None = None) -> TurnOutcome:
        """Persist the full outcome (all UI panels) so history survives reloads and restarts."""
        now = datetime.now().isoformat(timespec="seconds")
        with db.transaction() as conn:
            if user_input is None:
                conn.execute(
                    "UPDATE ui_turns SET status=?, outcome_json=?, updated_at=? WHERE turn_id=?",
                    (out.status, out.model_dump_json(), now, out.turn_id),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO ui_turns(turn_id, session_id, user_input, status,"
                    " outcome_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        out.turn_id,
                        out.session_id,
                        user_input,
                        out.status,
                        out.model_dump_json(),
                        now,
                        now,
                    ),
                )
        return out

    @staticmethod
    def session_history(session_id: str) -> list[dict[str, Any]]:
        """Stored turns of a session, oldest first: [{"input", "outcome"}]."""
        rows = db.query(
            "SELECT user_input, outcome_json FROM ui_turns WHERE session_id=? ORDER BY rowid",
            (session_id,),
        )
        return [{"input": r["user_input"], "outcome": json.loads(r["outcome_json"])} for r in rows]

    @staticmethod
    def list_sessions() -> list[str]:
        """Session IDs with stored turns, most recently active first."""
        rows = db.query(
            "SELECT session_id, MAX(updated_at) AS last FROM ui_turns "
            "GROUP BY session_id ORDER BY last DESC, MAX(rowid) DESC"
        )
        return [r["session_id"] for r in rows]

    def get_audit(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Audit rows, newest first."""
        return audit.get_audit(session_id)

    def get_memories(self, subject_id: str | None = None) -> list[dict[str, Any]]:
        """Long-term memories, newest first."""
        return memory.list_memories(subject_id)

    def delete_memory(self, memory_id: int) -> None:
        """Delete one memory."""
        memory.delete_memory(memory_id)

    def conversation(self, session_id: str) -> list[dict[str, str]]:
        """Completed exchanges stored in the session checkpoint."""
        snapshot = self.graph.get_state(self._config(session_id))
        return list((snapshot.values or {}).get("conversation") or [])
