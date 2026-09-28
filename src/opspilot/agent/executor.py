"""Dependency-aware parallel executor: runs independent plan steps concurrently, layer by layer."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from opspilot import audit
from opspilot.agent.schemas import PlanStep, StepResult
from opspilot.safety import preconditions
from opspilot.tools.registry import ToolContext, run_tool

MAX_WORKERS = 4


class CycleError(ValueError):
    """Raised when plan steps have circular dependencies."""


def topological_layers(steps: list[PlanStep]) -> list[list[PlanStep]]:
    """Group steps into layers; every step's dependencies are in earlier layers."""
    by_id = {s.id: s for s in steps}
    remaining = dict(by_id)
    done: set[str] = set()
    layers: list[list[PlanStep]] = []
    while remaining:
        ready = [
            s for s in remaining.values() if all(d in done or d not in by_id for d in s.depends_on)
        ]
        if not ready:
            raise CycleError(f"dependency cycle among steps {sorted(remaining)}")
        layers.append(ready)
        for s in ready:
            done.add(s.id)
            del remaining[s.id]
    return layers


def _run_step(step: PlanStep, ctx: ToolContext, iteration: int) -> StepResult:
    """Re-check preconditions, then run the tool, timing it."""
    start = time.time()
    check = preconditions.check(step.tool, step.args)
    if not check.ok:
        return StepResult(
            step_id=step.id,
            status="skipped",
            error="; ".join(check.reasons),
            tool=step.tool,
            args=step.args,
            iteration=iteration,
            started_at=start,
            finished_at=time.time(),
        )
    try:
        output = run_tool(step.tool, step.args, ctx)
        status = "executed" if output.get("ok") else "failed"
        error = None if output.get("ok") else output.get("summary")
    except Exception as exc:  # a crashing tool must not crash the turn
        output, status, error = {}, "failed", f"{type(exc).__name__}: {exc}"
    return StepResult(
        step_id=step.id,
        status=status,
        output=output,
        error=error,
        tool=step.tool,
        args=step.args,
        iteration=iteration,
        started_at=start,
        finished_at=time.time(),
    )


def execute_plan(
    steps: list[PlanStep],
    ctx: ToolContext,
    *,
    iteration: int = 1,
    prior: dict[str, StepResult] | None = None,
    approvers: dict[str, str] | None = None,
    risk_levels: dict[str, str] | None = None,
    on_result: Callable[[StepResult], None] | None = None,
) -> list[StepResult]:
    """Execute steps layer by layer. `prior` holds results already decided (skipped/rejected)."""
    results: dict[str, StepResult] = dict(prior or {})
    approvers = approvers or {}
    risk_levels = risk_levels or {}
    for layer in topological_layers(steps):
        runnable: list[PlanStep] = []
        for step in layer:
            if step.id in results:
                continue
            blocked = [
                d for d in step.depends_on if d in results and results[d].status != "executed"
            ]
            if blocked:
                results[step.id] = StepResult(
                    step_id=step.id,
                    status="skipped",
                    tool=step.tool,
                    args=step.args,
                    iteration=iteration,
                    error=f"dependency {blocked[0]} not executed",
                )
            else:
                runnable.append(step)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                s.id: pool.submit(_run_step, s, _ctx_for(ctx, approvers.get(s.id)), iteration)
                for s in runnable
            }
            for sid, fut in futures.items():
                results[sid] = fut.result()
        for step in layer:
            res = results[step.id]
            if prior and step.id in prior:
                continue
            _audit(step, res, ctx, risk_levels.get(step.id))
            if on_result:
                on_result(res)
    return [results[s.id] for s in steps]


def _ctx_for(ctx: ToolContext, approver: str | None) -> ToolContext:
    return ToolContext(ctx.session_id, ctx.turn_id, ctx.operator_id, approver)


def _audit(step: PlanStep, res: StepResult, ctx: ToolContext, risk: str | None) -> None:
    payload: dict[str, Any] = {
        "status": res.status,
        "error": res.error,
        "output": res.output,
        "started_at": res.started_at,
        "finished_at": res.finished_at,
    }
    audit.log_event(
        res.status,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        tool=step.tool,
        args=step.args,
        rationale=step.rationale if res.status == "executed" else (res.error or step.rationale),
        risk_level=risk,
        actor="system" if res.status == "skipped" else "agent",
        result=payload,
    )
