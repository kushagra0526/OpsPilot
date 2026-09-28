"""Pydantic models shared by retrieval, safety, the agent graph and the service layer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SourceType = Literal["policy", "db", "csv", "faq", "memory", "tool_result"]
DecisionType = Literal["answer", "act", "clarify", "escalate", "refuse"]
RiskLevel = Literal["low", "medium", "high"]


class Intent(BaseModel):
    """What the operator is asking for, with extracted entity IDs."""

    summary: str
    order_ids: list[str] = []
    customer_ids: list[str] = []
    emails: list[str] = []
    skus: list[str] = []
    request_type: Literal["question", "action", "mixed", "out_of_scope"] = "question"

    @property
    def has_entities(self) -> bool:
        """True when any concrete ID or email was found."""
        return bool(self.order_ids or self.customer_ids or self.emails or self.skus)


class Evidence(BaseModel):
    """One retrieved fact with a stable, citable ID."""

    id: str
    source_type: SourceType
    content: str
    metadata: dict[str, Any] = {}
    superseded: bool = False


class ConflictNote(BaseModel):
    """A detected conflict between sources and how it was resolved."""

    kind: Literal["policy_version", "inventory", "data"]
    detail: str
    resolution: str
    evidence_ids: list[str] = []


class RetrievalBundle(BaseModel):
    """Everything the planner may use: evidence, conflicts and known gaps."""

    evidence: list[Evidence] = []
    conflicts: list[ConflictNote] = []
    missing: list[str] = []
    max_similarity: float = 0.0
    entity_hits: int = 0

    def ids(self) -> set[str]:
        """All citable evidence IDs."""
        return {e.id for e in self.evidence}

    def get(self, evidence_id: str) -> Evidence | None:
        """Look up an evidence item by ID."""
        return next((e for e in self.evidence if e.id == evidence_id), None)

    def add(self, items: list[Evidence]) -> None:
        """Append evidence, skipping IDs already present."""
        known = self.ids()
        for item in items:
            if item.id not in known:
                self.evidence.append(item)
                known.add(item.id)

    def upsert(self, items: list[Evidence]) -> int:
        """Append new evidence and replace items whose content changed; returns how many changed."""
        changed = 0
        for item in items:
            index = next((i for i, e in enumerate(self.evidence) if e.id == item.id), None)
            if index is None:
                self.evidence.append(item)
                changed += 1
            elif self.evidence[index].content != item.content:
                self.evidence[index] = item
                changed += 1
        return changed


class PlanStep(BaseModel):
    """One proposed tool call with its justification."""

    id: str
    tool: str
    args: dict[str, Any] = {}
    depends_on: list[str] = []
    rationale: str
    citations: list[str] = []
    expected_outcome: str = ""


class AgentDecision(BaseModel):
    """The planner's structured output."""

    decision_type: DecisionType
    analysis: str
    conflicts_considered: list[str] = []
    missing_information: list[str] = []
    steps: list[PlanStep] = []
    answer_draft: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("steps", mode="before")
    @classmethod
    def _none_steps(cls, value: Any) -> Any:
        return value or []


class StepResult(BaseModel):
    """Outcome of one plan step."""

    step_id: str
    status: Literal["executed", "failed", "skipped", "rejected"]
    output: dict[str, Any] = {}
    error: str | None = None
    tool: str = ""
    args: dict[str, Any] = {}
    iteration: int = 1
    started_at: float | None = None
    finished_at: float | None = None


class RiskAssessment(BaseModel):
    """Deterministic risk verdict for a step."""

    level: RiskLevel
    requires_approval: bool
    reasons: list[str] = []


class ApprovalDecision(BaseModel):
    """An operator's verdict on one pending step."""

    approved: bool
    approver: str = "operator-1"
    note: str = ""


class TurnOutcome(BaseModel):
    """What the service returns to the UI/CLI after a turn or a resume."""

    session_id: str
    turn_id: str
    status: Literal["completed", "awaiting_approval"]
    final_response: str | None = None
    decision: dict[str, Any] | None = None
    plan_history: list[dict[str, Any]] = []
    pending_approvals: list[dict[str, Any]] = []
    risk_assessments: dict[str, dict[str, Any]] = {}
    step_results: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    missing: list[str] = []
    evidence_used: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
