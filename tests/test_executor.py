"""Dependency-aware parallel executor (SPEC §8.4)."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from pydantic import BaseModel

from opspilot.agent.executor import CycleError, execute_plan, topological_layers
from opspilot.agent.schemas import PlanStep, StepResult
from opspilot.tools.registry import REGISTRY, ToolContext, ToolSpec, register


class SleepArgs(BaseModel):
    """Args for the test-only slow tool."""

    seconds: float = 0.3
    fail: bool = False


def _sleep(args: SleepArgs, ctx: ToolContext) -> dict:
    time.sleep(args.seconds)
    return {"ok": not args.fail, "summary": "slept", "data": {}}


@pytest.fixture()
def slow_tool() -> Iterator[None]:
    register(ToolSpec("test_sleep", "test only", SleepArgs, _sleep, "action", "low", True))
    yield
    REGISTRY.pop("test_sleep", None)


def _s(sid: str, deps: list[str] | None = None, **args) -> PlanStep:
    return PlanStep(id=sid, tool="test_sleep", args=args, depends_on=deps or [], rationale="r")


CTX = ToolContext("exec-session", "t1")


def test_independent_steps_overlap(fresh_db, slow_tool) -> None:
    start = time.time()
    a, b = execute_plan([_s("s1"), _s("s2")], CTX)
    assert a.status == b.status == "executed"
    assert a.started_at < b.finished_at and b.started_at < a.finished_at, "steps should overlap"
    assert time.time() - start < 0.55, "two 0.3s steps in parallel should take ~0.3s"


def test_dependent_step_waits(fresh_db, slow_tool) -> None:
    a, b = execute_plan([_s("s1"), _s("s2", ["s1"])], CTX)
    assert b.started_at >= a.finished_at


def test_failed_dependency_skips_dependent(fresh_db, slow_tool) -> None:
    a, b, c = execute_plan(
        [_s("s1", fail=True, seconds=0.01), _s("s2", ["s1"], seconds=0.01), _s("s3", seconds=0.01)],
        CTX,
    )
    assert a.status == "failed"
    assert b.status == "skipped" and b.error == "dependency s1 not executed"
    assert c.status == "executed"


def test_prior_rejection_skips_dependent(fresh_db, slow_tool) -> None:
    prior = {"s1": StepResult(step_id="s1", status="rejected", tool="test_sleep")}
    a, b = execute_plan([_s("s1"), _s("s2", ["s1"], seconds=0.01)], CTX, prior=prior)
    assert a.status == "rejected" and b.status == "skipped"


def test_layers_and_cycle_detection() -> None:
    layers = topological_layers([_s("s3", ["s1", "s2"]), _s("s1"), _s("s2", ["s1"])])
    assert [[s.id for s in layer] for layer in layers] == [["s1"], ["s2"], ["s3"]]
    with pytest.raises(CycleError):
        topological_layers([_s("s1", ["s2"]), _s("s2", ["s1"])])
