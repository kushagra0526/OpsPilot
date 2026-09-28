"""Graph node functions. Each takes the state and returns a partial update."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel

from opspilot import audit, memory
from opspilot.agent import prompts
from opspilot.agent.citations import cited_ids, humanize, label_for
from opspilot.agent.executor import execute_plan, topological_layers
from opspilot.agent.schemas import (
    AgentDecision,
    ApprovalDecision,
    Evidence,
    Intent,
    PlanStep,
    RetrievalBundle,
    StepResult,
)
from opspilot.agent.state import AgentState
from opspilot.config import MAX_ITERATIONS, today
from opspilot.knowledge import retriever, structured
from opspilot.llm import LLMClient, LLMError, complete_structured
from opspilot.safety import preconditions, risk
from opspilot.safety.grounding import relevance_signal, validate_decision
from opspilot.tools.registry import ToolContext, get_tool

ORDER_RE = re.compile(r"\bORD-\d{4}\b")
CUSTOMER_RE = re.compile(r"\bC-\d{3}\b")
SKU_RE = re.compile(r"\b[A-Z]{2}-[A-Z]{3}-\d{2}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
NON_REPLAN_PREFIXES = ("dependency", "already executed", "no change needed")


class _IntentLLM(BaseModel):
    summary: str = ""
    request_type: Literal["question", "action", "mixed", "out_of_scope"] | None = None


def _empty_usage() -> dict[str, Any]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "retries": 0,
        "by_node": {},
        "tokens_by_tier": {},
    }


def _bundle(state: AgentState) -> RetrievalBundle:
    return RetrievalBundle.model_validate(state.get("bundle") or {})


def _decision(state: AgentState) -> AgentDecision:
    return AgentDecision.model_validate(state["decision"])


def _current_results(state: AgentState) -> list[StepResult]:
    it = state.get("iteration", 1)
    return [
        StepResult.model_validate(r) for r in state.get("step_results", []) if r["iteration"] == it
    ]


def _refresh_records(bundle: RetrievalBundle, results: list[StepResult]) -> int:
    """Re-read the orders and customers that blocked or failed steps refer to; returns changes."""
    order_ids = {r.args["order_id"] for r in results if r.args.get("order_id")}
    customer_ids = {r.args["customer_id"] for r in results if r.args.get("customer_id")}
    fresh: list[Evidence] = []
    for oid in sorted(order_ids):
        if order := structured.get_order(oid):
            fresh.append(structured.order_evidence(order))
            customer_ids.add(order["customer_id"])
    for cid in sorted(customer_ids):
        if customer := structured.get_customer(customer_id=cid):
            fresh.append(structured.customer_evidence(customer))
    return bundle.upsert(fresh)


def runnable_steps(state: AgentState) -> list[PlanStep]:
    """Steps of the current decision that have no result yet in this iteration."""
    if not state.get("decision"):
        return []
    done = {r.step_id for r in _current_results(state)}
    return [s for s in _decision(state).steps if s.id not in done]


class AgentNodes:
    """Node implementations bound to one LLM client."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    # ---- bookkeeping -------------------------------------------------------------------
    def _finish(self, state: AgentState, node: str, summary: str, **updates: Any) -> dict[str, Any]:
        """Attach a trace event and fold LLM usage (and retries) into the state."""
        usage = {**_empty_usage(), **(state.get("usage") or {})}
        session = {**_empty_usage(), **(state.get("session_usage") or {})}
        trace = list(state.get("trace") or [])
        for call in self.llm.drain_calls():
            if call["event"] == "retry":
                usage["retries"] += 1
                session["retries"] += 1
                trace.append(
                    self._event(
                        state,
                        node,
                        (
                            f"LLM rate-limited/unavailable ({call['error']}); retrying… attempt "
                            f"{call['attempt']} after {call['delay']}s"
                        ),
                    )
                )
                continue
            for u in (usage, session):
                u["requests"] += 1
                u["input_tokens"] += call.get("input_tokens", 0)
                u["output_tokens"] += call.get("output_tokens", 0)
                u["by_node"] = {**u["by_node"], node: u["by_node"].get(node, 0) + 1}
                tier = call.get("tier", "unknown")
                spent = call.get("input_tokens", 0) + call.get("output_tokens", 0)
                tiers = u.get("tokens_by_tier") or {}
                u["tokens_by_tier"] = {**tiers, tier: tiers.get(tier, 0) + spent}
        trace.append(self._event(state, node, summary))
        return {**updates, "usage": usage, "session_usage": session, "trace": trace}

    @staticmethod
    def _event(state: AgentState, node: str, summary: str) -> dict[str, Any]:
        return {
            "node": node,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "summary": summary,
            "turn_id": state.get("turn_id"),
        }

    def _ctx(self, state: AgentState) -> ToolContext:
        return ToolContext(
            state["session_id"], state["turn_id"], state.get("operator_id", "operator-1")
        )

    # ---- nodes -------------------------------------------------------------------------
    def understand(self, state: AgentState) -> dict[str, Any]:
        """Regex entity extraction + small-model summary/request type (regex wins on IDs)."""
        text = state["user_input"]
        skus = list(dict.fromkeys(SKU_RE.findall(text) + structured.find_skus_by_name(text)))
        order_ids = list(dict.fromkeys(ORDER_RE.findall(text)))
        customer_ids = list(dict.fromkeys(CUSTOMER_RE.findall(text)))
        emails = list(dict.fromkeys(EMAIL_RE.findall(text)))
        try:
            llm_out = complete_structured(
                self.llm,
                "understand",
                prompts.UNDERSTAND_SYSTEM,
                prompts.build_understand_user(text),
                _IntentLLM,
                tier="small",
            )
        except LLMError:
            llm_out = _IntentLLM()
        has_ids = bool(order_ids or customer_ids or emails or skus)
        request_type = llm_out.request_type or ("action" if has_ids else "question")
        if request_type == "out_of_scope" and has_ids:
            request_type = "mixed"
        intent = Intent(
            summary=llm_out.summary or text,
            order_ids=order_ids,
            customer_ids=customer_ids,
            emails=emails,
            skus=skus,
            request_type=request_type,
        )
        found = ", ".join(order_ids + customer_ids + emails + skus) or "no IDs"
        return self._finish(
            state, "understand", f"{request_type}: {found}", intent=intent.model_dump()
        )

    def load_memory(self, state: AgentState) -> dict[str, Any]:
        """Load memories for customers named directly or via their orders/emails, plus the operator."""
        intent = Intent.model_validate(state["intent"])
        customers = list(intent.customer_ids) + structured.customer_ids_for_orders(intent.order_ids)
        for email in intent.emails:
            cust = structured.get_customer(email=email)
            if cust:
                customers.append(cust["id"])
        mems = memory.load_memories(customers, state.get("operator_id"))
        return self._finish(state, "load_memory", f"{len(mems)} memories loaded", memories=mems)

    def retrieve(self, state: AgentState) -> dict[str, Any]:
        """Parallel retrieval across Chroma, SQLite, CSV and FAQ."""
        start = time.time()
        intent = Intent.model_validate(state["intent"])
        bundle = retriever.retrieve(intent, state["user_input"], state.get("memories") or [])
        counts: dict[str, int] = {}
        for e in bundle.evidence:
            counts[e.source_type] = counts.get(e.source_type, 0) + 1
        summary = (
            f"{len(bundle.evidence)} evidence items {counts}, {len(bundle.conflicts)} conflicts, "
            f"{len(bundle.missing)} missing, in {time.time() - start:.2f}s"
        )
        return self._finish(state, "retrieve", summary, bundle=bundle.model_dump())

    def _attempts(self, state: AgentState) -> list[dict[str, Any]]:
        results = state.get("step_results") or []
        return [
            {
                "iteration": r["iteration"],
                "step": r["step_id"],
                "tool": r["tool"],
                "args": r["args"],
                "status": r["status"],
                "result": (r.get("output") or {}).get("summary"),
                "reason": r.get("error"),
            }
            for r in results
        ]

    def reason_and_plan(self, state: AgentState) -> dict[str, Any]:
        """Large model produces a structured AgentDecision over the evidence (including refusals)."""
        intent = Intent.model_validate(state["intent"])
        bundle = _bundle(state)
        retry = bool(state.get("grounding_retry"))
        iteration = state.get("iteration", 0) + (0 if retry else 1)
        user = prompts.build_plan_user(
            user_input=state["user_input"],
            intent=intent.model_dump(),
            bundle=bundle,
            iteration=iteration,
            today=today().isoformat(),
            relevance=relevance_signal(intent, bundle),
            conversation=state.get("conversation") or [],
            attempts=self._attempts(state),
            validation_errors=state.get("validation_errors") or [] if retry else [],
        )
        try:
            decision = complete_structured(
                self.llm, "plan", prompts.PLAN_SYSTEM, user, AgentDecision, tier="large"
            )
        except LLMError as exc:
            decision = AgentDecision(
                decision_type="clarify",
                confidence=0.0,
                analysis=f"Planning failed: {exc}",
                answer_draft=(
                    "I couldn't complete the reasoning step because the language model "
                    "returned an unusable answer or was unavailable. No action was taken; "
                    "please retry."
                ),
            )
        summary = (
            f"iteration {iteration}{' (grounding retry)' if retry else ''}: "
            f"{decision.decision_type}, {len(decision.steps)} steps, "
            f"confidence {decision.confidence:.2f}"
        )
        return self._finish(
            state, "reason_and_plan", summary, decision=decision.model_dump(), iteration=iteration
        )

    def validate(self, state: AgentState) -> dict[str, Any]:
        """Grounding + schema checks, then risk classification and preconditions per step."""
        decision, bundle = _decision(state), _bundle(state)
        sid, tid = state["session_id"], state["turn_id"]
        it = state.get("iteration", 1)
        if decision.decision_type == "refuse":
            decision.steps = []
            audit.log_event(
                "refused",
                session_id=sid,
                turn_id=tid,
                actor="agent",
                rationale=f"{state['user_input']} :: {decision.analysis}",
            )
            return self._finish(
                state,
                "validate",
                "refused; audit event logged",
                decision=decision.model_dump(),
                grounding_retry=False,
                validation_errors=[],
                plan_history=[
                    *state.get("plan_history", []),
                    {"iteration": it, "decision": decision.model_dump()},
                ],
            )
        errors = validate_decision(decision, bundle)
        if errors and not state.get("grounding_retry"):
            return self._finish(
                state,
                "validate",
                f"{len(errors)} grounding errors; re-prompting once",
                grounding_retry=True,
                validation_errors=errors,
            )
        if errors:
            kind = "escalate" if (bundle.missing or bundle.conflicts) else "clarify"
            decision = AgentDecision(
                decision_type=kind,
                confidence=0.0,
                steps=[],
                analysis=(
                    "I could not produce a plan fully grounded in the retrieved evidence, so "
                    "no action was taken. Validation errors: " + "; ".join(errors[:5])
                ),
                answer_draft=(
                    "I couldn't build a plan that is fully supported by NovaMart's internal "
                    "knowledge. Please review the case manually or rephrase the request."
                ),
            )
        risks: dict[str, dict[str, Any]] = {}
        results = list(state.get("step_results") or [])
        executed_before = {
            (r["tool"], repr(sorted(r["args"].items())))
            for r in results
            if r["status"] == "executed"
        }
        blocked: set[str] = set()
        for layer in topological_layers(decision.steps):
            for step in layer:
                assessment = risk.classify(step, decision.confidence)
                risks[step.id] = assessment.model_dump()
                audit.log_event(
                    "proposed",
                    session_id=sid,
                    turn_id=tid,
                    tool=step.tool,
                    args=step.args,
                    rationale=step.rationale,
                    risk_level=assessment.level,
                    actor="agent",
                    result={"risk_reasons": assessment.reasons, "iteration": it},
                )
                reason = self._precheck(step, blocked, executed_before)
                if reason:
                    blocked.add(step.id)
                    results.append(
                        StepResult(
                            step_id=step.id,
                            status="skipped",
                            error=reason,
                            tool=step.tool,
                            args=step.args,
                            iteration=it,
                        ).model_dump()
                    )
                    audit.log_event(
                        "skipped",
                        session_id=sid,
                        turn_id=tid,
                        tool=step.tool,
                        args=step.args,
                        rationale=reason,
                        risk_level=assessment.level,
                        actor="system",
                    )
        skipped = len(blocked)
        summary = (
            f"{'fallback ' if errors else ''}{decision.decision_type}: {len(decision.steps)} steps "
            f"proposed, {skipped} blocked by preconditions"
        )
        return self._finish(
            state,
            "validate",
            summary,
            decision=decision.model_dump(),
            risk_assessments=risks,
            step_results=results,
            grounding_retry=False,
            validation_errors=[],
            plan_history=[
                *state.get("plan_history", []),
                {"iteration": it, "decision": decision.model_dump()},
            ],
        )

    @staticmethod
    def _precheck(
        step: PlanStep, blocked: set[str], executed_before: set[tuple[str, str]]
    ) -> str | None:
        dep = next((d for d in step.depends_on if d in blocked), None)
        if dep:
            return f"dependency {dep} not executed"
        if (step.tool, repr(sorted(step.args.items()))) in executed_before:
            return "already executed earlier in this turn"
        check = preconditions.check(step.tool, step.args)
        return None if check.ok else "; ".join(check.reasons)

    def approval_gate(self, state: AgentState) -> dict[str, Any]:
        """Collect runnable steps that code says need approval; log approval_requested."""
        risks = state.get("risk_assessments") or {}
        pending = []
        for step in runnable_steps(state):
            assessment = risks.get(step.id, {})
            if assessment.get("requires_approval"):
                pending.append(
                    {
                        "step_id": step.id,
                        "tool": step.tool,
                        "args": step.args,
                        "rationale": step.rationale,
                        "citations": step.citations,
                        "risk_level": assessment["level"],
                        "reasons": assessment["reasons"],
                    }
                )
                audit.log_event(
                    "approval_requested",
                    session_id=state["session_id"],
                    turn_id=state["turn_id"],
                    tool=step.tool,
                    args=step.args,
                    rationale="; ".join(assessment["reasons"]),
                    risk_level=assessment["level"],
                    actor="agent",
                )
        summary = f"{len(pending)} step(s) need human approval" if pending else "no approval needed"
        return self._finish(state, "approval_gate", summary, pending_approvals=pending)

    def await_approval(self, state: AgentState) -> dict[str, Any]:
        """Pause via interrupt(); on resume, record each approve/reject decision."""
        pending = state.get("pending_approvals") or []
        answers = (
            interrupt(
                {"pending": pending, "session_id": state["session_id"], "turn_id": state["turn_id"]}
            )
            or {}
        )
        approvals = dict(state.get("approvals") or {})
        results = list(state.get("step_results") or [])
        it = state.get("iteration", 1)
        verdicts = []
        for p in pending:
            raw = answers.get(p["step_id"]) or {
                "approved": False,
                "approver": "system",
                "note": "no decision provided",
            }
            d = raw if isinstance(raw, ApprovalDecision) else ApprovalDecision.model_validate(raw)
            event = "approved" if d.approved else "rejected"
            audit.log_event(
                event,
                session_id=state["session_id"],
                turn_id=state["turn_id"],
                tool=p["tool"],
                args=p["args"],
                rationale=d.note or p["rationale"],
                risk_level=p["risk_level"],
                actor=d.approver,
            )
            approvals[p["step_id"]] = {**d.model_dump(), "iteration": it}
            verdicts.append(f"{p['step_id']} {event} by {d.approver}")
            if not d.approved:
                note = f": {d.note}" if d.note else ""
                results.append(
                    StepResult(
                        step_id=p["step_id"],
                        status="rejected",
                        tool=p["tool"],
                        args=p["args"],
                        iteration=it,
                        error=f"rejected by {d.approver}{note}",
                    ).model_dump()
                )
        return self._finish(
            state,
            "await_approval",
            "; ".join(verdicts) or "nothing pending",
            approvals=approvals,
            step_results=results,
            pending_approvals=[],
        )

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Run the approved/auto steps with the dependency-aware parallel executor."""
        decision = _decision(state)
        it = state.get("iteration", 1)
        prior = {r.step_id: r for r in _current_results(state)}
        approvals = state.get("approvals") or {}
        approvers = {
            sid: a["approver"]
            for sid, a in approvals.items()
            if a.get("approved") and a.get("iteration") == it
        }
        risk_levels = {sid: r["level"] for sid, r in (state.get("risk_assessments") or {}).items()}
        results = execute_plan(
            decision.steps,
            self._ctx(state),
            iteration=it,
            prior=prior,
            approvers=approvers,
            risk_levels=risk_levels,
        )
        new = [r for r in results if r.step_id not in prior]
        all_results = list(state.get("step_results") or []) + [r.model_dump() for r in new]
        summary = ", ".join(f"{r.step_id}:{r.tool}={r.status}" for r in new) or "nothing to run"
        overlap = _parallel_note(new)
        return self._finish(state, "execute", summary + overlap, step_results=all_results)

    def observe(self, state: AgentState) -> dict[str, Any]:
        """Fold read-tool results into the evidence; decide whether to replan."""
        bundle = _bundle(state)
        current = _current_results(state)
        new_info = False
        for r in current:
            if r.status == "executed" and (spec := get_tool(r.tool)) and spec.kind == "read":
                bundle.upsert([Evidence.model_validate(e) for e in r.output.get("evidence", [])])
                new_info = True
        problems = [
            r
            for r in current
            if r.status == "failed"
            or (r.status == "skipped" and not (r.error or "").startswith(NON_REPLAN_PREFIXES))
        ]
        # A step can be blocked because the data changed after the plan was made (e.g. another
        # operator refunded the order while this plan waited for approval): replan on current records.
        refreshed = _refresh_records(bundle, problems)
        it = state.get("iteration", 1)
        wants = bool(problems) or new_info
        replan = wants and it < MAX_ITERATIONS
        if replan:
            summary = (
                f"replanning (iteration {it + 1}): {len(problems)} blocked/failed, "
                f"new info={new_info}, records refreshed={refreshed}"
            )
        elif wants:
            summary = f"iteration cap {MAX_ITERATIONS} reached; responding with what is known"
        else:
            summary = "all steps settled; responding"
        return self._finish(
            state, "observe", summary, bundle=bundle.model_dump(), needs_replan=replan
        )

    def respond(self, state: AgentState) -> dict[str, Any]:
        """Operator-facing explanation with readable citations."""
        decision, bundle = _decision(state), _bundle(state)
        results = [r for r in state.get("step_results") or []]
        if decision.decision_type == "refuse" and (decision.answer_draft or "").strip():
            text = decision.answer_draft or ""  # the planner's own reply to an off-topic request
        else:
            ids = cited_ids([h["decision"] for h in state.get("plan_history", [])], bundle)
            lines = [f"[{i}] = {label_for(i, bundle)}: {bundle.get(i).content[:300]}" for i in ids]  # type: ignore[union-attr]
            text = ""
            try:
                text = self.llm.complete(
                    "respond",
                    prompts.RESPOND_SYSTEM,
                    prompts.build_respond_user(
                        user_input=state["user_input"],
                        decision=decision.model_dump(),
                        results=results,
                        evidence_lines=lines,
                        conflicts=[f"{c.detail} {c.resolution}" for c in bundle.conflicts],
                    ),
                    tier="large",
                    temperature=0.3,
                    json_mode=False,
                ).strip()
            except LLMError:
                text = ""
            if not text:
                text = render_fallback(decision, results, bundle, ids)
        final = humanize(text, bundle)
        convo = [
            *(state.get("conversation") or []),
            {"user": state["user_input"], "assistant": final},
        ]
        return self._finish(
            state, "respond", f"{len(final)} chars", final_response=final, conversation=convo[-10:]
        )

    def save_memory(self, state: AgentState) -> dict[str, Any]:
        """Extract durable facts (small model) and store an interaction summary per customer."""
        bundle = _bundle(state)
        intent = Intent.model_validate(state["intent"])
        customers = list(
            dict.fromkeys(
                intent.customer_ids
                + [e.id.split(":")[2] for e in bundle.evidence if e.id.startswith("db:customers:")]
            )
        )
        decision = _decision(state)
        done = [r["tool"] for r in state.get("step_results") or [] if r["status"] == "executed"]
        fallback = f"{intent.summary[:160]} -> {decision.decision_type}" + (
            f"; executed {', '.join(done)}" if done else "; no actions executed"
        )
        saved = memory.extract_and_save(
            self.llm,
            session_id=state["session_id"],
            user_input=state["user_input"],
            final_response=state.get("final_response") or "",
            customer_ids=customers,
            operator_id=state.get("operator_id", "operator-1"),
            fallback_summary=fallback,
        )
        new = [m for m in saved if m["new"]]
        return self._finish(state, "save_memory", f"{len(new)} new memories", memories_saved=saved)


def _parallel_note(results: list[StepResult]) -> str:
    timed = [r for r in results if r.started_at and r.finished_at]
    for i, a in enumerate(timed):
        for b in timed[i + 1 :]:
            if a.started_at < b.finished_at and b.started_at < a.finished_at:  # type: ignore[operator]
                return f" (parallel: {a.step_id} ∥ {b.step_id})"
    return ""


def render_fallback(
    decision: AgentDecision, results: list[dict[str, Any]], bundle: RetrievalBundle, ids: list[str]
) -> str:
    """Deterministic operator message used when no response model output is available."""
    headline = {
        "answer": "Answer",
        "act": "Actions",
        "escalate": "Escalated to a human",
        "clarify": "More information needed",
        "refuse": "Out of scope",
    }[decision.decision_type]
    parts = [f"### {headline}"]
    if decision.answer_draft:
        parts += [decision.answer_draft]
    done = [r for r in results if r["status"] == "executed"]
    not_done = [r for r in results if r["status"] != "executed"]
    if done:
        parts += ["", "**Done:**"] + [
            f"- {(r.get('output') or {}).get('summary', r['tool'])}" for r in done
        ]
    if not_done:
        parts += ["", "**Not done:**"] + [
            f"- `{r['tool']}` was {r['status']}: {r.get('error')}" for r in not_done
        ]
    parts += ["", f"**Why:** {decision.analysis}"]
    if ids:
        parts += ["", "**Sources:** " + "; ".join(label_for(i, bundle) for i in ids)]
    return "\n".join(parts)
