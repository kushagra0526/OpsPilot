"""The eval scorer detects failures (not just passes)."""

from __future__ import annotations

import sys

from opspilot.agent.schemas import TurnOutcome
from opspilot.config import ROOT_DIR

sys.path.insert(0, str(ROOT_DIR / "evals"))
from run_evals import score  # noqa: E402

SCENARIO = {
    "id": 2,
    "name": "x",
    "expected_decision_type": "act",
    "expected_tools": ["issue_refund", "send_customer_email"],
    "must_require_approval": False,
    "must_not_tools": ["cancel_order"],
    "email_depends_on_refund": True,
}


def _outcome(
    tools: list[str], decision: str = "act", email_deps: list[str] | None = None
) -> TurnOutcome:
    steps = [
        {
            "id": f"s{i}",
            "tool": t,
            "args": {},
            "depends_on": email_deps or [] if t == "send_customer_email" else [],
        }
        for i, t in enumerate(tools, 1)
    ]
    results = [
        {"step_id": s["id"], "tool": s["tool"], "status": "executed", "args": {}, "output": {}}
        for s in steps
    ]
    return TurnOutcome(
        session_id="s",
        turn_id="t",
        status="completed",
        decision={"decision_type": decision, "steps": steps},
        plan_history=[{"iteration": 1, "decision": {"decision_type": decision, "steps": steps}}],
        step_results=results,
    )


def _failed(checks) -> set[str]:
    return {name for name, ok, _ in checks if not ok}


def test_all_checks_pass() -> None:
    run = {
        "out": _outcome(["issue_refund", "send_customer_email"], email_deps=["s1"]),
        "approval_requested": False,
        "session": "s",
    }
    assert _failed(score(SCENARIO, run)) == set()


def test_failures_are_detected() -> None:
    run = {
        "out": _outcome(["cancel_order", "send_customer_email"], decision="answer"),
        "approval_requested": True,
        "session": "s",
    }
    assert _failed(score(SCENARIO, run)) == {
        "decision_type",
        "expected_tools",
        "approval",
        "must_not_tools",
        "email_after_refund",
    }


def test_refund_order_checks() -> None:
    sc = {
        **SCENARIO,
        "expected_refund_orders": ["ORD-1007"],
        "must_not_refund_orders": ["ORD-1002"],
    }
    out = _outcome(["issue_refund"])
    out.step_results[0]["args"] = {"order_id": "ORD-1002"}
    failed = _failed(score(sc, {"out": out, "approval_requested": False, "session": "s"}))
    assert {"refunded_orders", "not_refunded_orders"} <= failed


def test_tier_totals() -> None:
    from run_evals import tier_totals

    rows = [
        {"tokens": 10, "tokens_by_tier": {"large": 7, "small": 3}},
        {"tokens": 5, "tokens_by_tier": {"large": 5}},
    ]
    assert tier_totals(rows) == {"tokens_total": 15, "tokens_large": 12, "tokens_small": 3}


def test_runs_combine_only_on_matching_fingerprint(tmp_path, monkeypatch) -> None:
    import run_evals

    monkeypatch.setattr(run_evals, "RUNS_DIR", tmp_path)
    row = {
        "id": 1,
        "name": "x",
        "checks": [["decision_type", True, "ok"]],
        "passed": 1,
        "total": 1,
        "latency": 1.0,
        "calls": 4,
        "tokens": 10,
        "tokens_by_tier": {"large": 8, "small": 2},
    }
    fp = run_evals.code_fingerprint("main")
    assert fp == run_evals.code_fingerprint("main"), "fingerprint is deterministic"
    for day, f in [("28", fp), ("29", "stale-code"), ("30", fp)]:
        meta = {
            "started": f"2026-09-{day}T10:00:00",
            "finished": "x",
            "git": "g",
            "fingerprint": f,
            "model": {},
        }
        run_evals.save_run("main", [row], meta)
    stored = run_evals.stored_runs("main", fp)
    assert [r["meta"]["started"][:10] for r in stored] == ["2026-09-28", "2026-09-30"]
    report = run_evals.render_report(
        [r["rows"] for r in stored],
        title="Main",
        mode="live",
        model={},
        started="s",
        git="g",
        planned=1,
        run_meta=[r["meta"] for r in stored],
    )
    assert "Run 1: 1/1 scenarios fully passing" in report and "Stability across runs" in report
