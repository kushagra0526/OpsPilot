"""Grounding guardrails: citations must point at retrieved, non-superseded evidence; relevance signal."""

from __future__ import annotations

import re

from opspilot.agent.schemas import AgentDecision, Intent, RetrievalBundle
from opspilot.config import RELEVANCE_THRESHOLD
from opspilot.tools.registry import validate_args

_BRACKETS = re.compile(r"\[([^\[\]]+)\]")
_EVIDENCE_ID = re.compile(r"^(policy|db|csv|faq|memory|tool_result):\S+$")


def normalize_brackets(text: str | None) -> str:
    """Models sometimes cite with fullwidth 【…】 or 〔…〕 brackets; treat them as [ ]."""
    return (text or "").translate(str.maketrans("【〔［】〕］", "[[[]]]"))


def extract_citations(text: str | None) -> list[str]:
    """Evidence IDs cited in [brackets] (comma/semicolon separated lists allowed)."""
    found: list[str] = []
    for group in _BRACKETS.findall(normalize_brackets(text)):
        for token in re.split(r"[,;]\s*", group):
            token = token.strip()
            if _EVIDENCE_ID.match(token):
                found.append(token)
    return found


def relevance_signal(intent: Intent, bundle: RetrievalBundle) -> str:
    """How well the request matched NovaMart's knowledge: an input to the planner, not a decision."""
    related = (
        bundle.entity_hits > 0
        or intent.has_entities
        or bundle.max_similarity >= RELEVANCE_THRESHOLD
    )
    return (
        f"NovaMart records matched: {bundle.entity_hits}. Best knowledge-base similarity: "
        f"{bundle.max_similarity:.2f} (requests below {RELEVANCE_THRESHOLD:.2f} that match no "
        f"record are usually not about NovaMart). Signal: {'related' if related else 'likely unrelated'}."
    )


def _cycle_errors(decision: AgentDecision) -> list[str]:
    from opspilot.agent.executor import CycleError, topological_layers

    try:
        topological_layers(decision.steps)
    except CycleError as exc:
        return [str(exc)]
    return []


def validate_decision(decision: AgentDecision, bundle: RetrievalBundle) -> list[str]:
    """Return a list of grounding/plan errors (empty = valid)."""
    errors: list[str] = []
    known = bundle.ids()

    def unknown(ids: list[str]) -> list[str]:
        return [i for i in ids if i not in known]

    needs_grounding = decision.decision_type in ("answer", "act", "escalate")
    analysis_cites = extract_citations(decision.analysis)
    draft_cites = extract_citations(decision.answer_draft)
    if needs_grounding and not (analysis_cites or draft_cites):
        errors.append("analysis must cite at least one evidence ID in [brackets]")
    for bad in unknown(analysis_cites + draft_cites):
        errors.append(f"citation [{bad}] is not in the EVIDENCE block")
    if decision.decision_type == "act" and not decision.steps:
        errors.append("decision_type 'act' requires at least one step")

    step_ids = [s.id for s in decision.steps]
    if len(set(step_ids)) != len(step_ids):
        errors.append("step ids must be unique")
    for step in decision.steps:
        try:
            validate_args(step.tool, step.args)
        except ValueError as exc:
            errors.append(f"step {step.id}: {exc}")
        cites = list(dict.fromkeys(step.citations + extract_citations(step.rationale)))
        if not cites:
            errors.append(f"step {step.id}: rationale must cite at least one evidence ID")
        for bad in unknown(cites):
            errors.append(f"step {step.id}: citation [{bad}] is not in the EVIDENCE block")
        for cid in cites:
            ev = bundle.get(cid)
            if ev is not None and ev.superseded:
                errors.append(
                    f"step {step.id}: [{cid}] is SUPERSEDED and cannot be the basis for an action"
                )
        for dep in step.depends_on:
            if dep not in step_ids:
                errors.append(f"step {step.id}: depends_on unknown step {dep}")
    if not errors:
        errors.extend(_cycle_errors(decision))
    return errors
