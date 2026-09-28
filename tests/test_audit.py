"""Audit trail: roundtrip, and every executed/skipped action writes a row."""

from __future__ import annotations

import json

import pytest

from opspilot import audit
from opspilot.agent.executor import execute_plan
from opspilot.agent.schemas import PlanStep
from opspilot.tools.registry import ToolContext


def test_log_and_read_roundtrip(fresh_db) -> None:
    audit.log_event(
        "proposed",
        session_id="s-a",
        turn_id="t1",
        tool="issue_refund",
        args={"amount": 1},
        rationale="why",
        risk_level="medium",
    )
    audit.log_event("refused", session_id="s-b", turn_id="t1", rationale="off-domain")
    rows = audit.get_audit("s-a")
    assert len(rows) == 1 and rows[0]["event"] == "proposed"
    assert json.loads(rows[0]["args_json"]) == {"amount": 1}
    assert [r["event"] for r in audit.get_audit()][:2] == ["refused", "proposed"]


def test_unknown_event_rejected(fresh_db) -> None:
    with pytest.raises(ValueError):
        audit.log_event("hacked", session_id="s", turn_id="t")


ACTIONS = [
    ("issue_refund", {"order_id": "ORD-1007", "amount": 149, "reason": "x"}),
    ("cancel_order", {"order_id": "ORD-1008", "reason": "x"}),
    ("issue_coupon", {"customer_id": "C-001", "percent_off": 10, "reason": "x"}),
    (
        "create_ticket",
        {"order_id": "ORD-1003", "category": "shipping_investigation", "summary": "x"},
    ),
    ("escalate_to_human", {"order_id": "ORD-1004", "summary": "x"}),
    ("send_customer_email", {"customer_id": "C-001", "subject": "s", "body": "b"}),
    ("update_order_status", {"order_id": "ORD-1003", "new_status": "delivered"}),
]


@pytest.mark.parametrize(("tool", "args"), ACTIONS)
def test_every_action_is_audited(fresh_db, tool: str, args: dict) -> None:
    step = PlanStep(id="s1", tool=tool, args=args, rationale="because [db:orders:ORD-1001]")
    [res] = execute_plan([step], ToolContext("s-audit", f"t-{tool}"), risk_levels={"s1": "low"})
    assert res.status == "executed", res.error
    rows = audit.get_audit("s-audit", f"t-{tool}")
    assert [r["event"] for r in rows] == ["executed"]
    assert rows[0]["tool"] == tool and rows[0]["rationale"] and rows[0]["risk_level"] == "low"


def test_failed_precondition_is_audited_as_skipped(fresh_db) -> None:
    step = PlanStep(
        id="s1",
        tool="issue_refund",
        rationale="r",
        args={"order_id": "ORD-1005", "amount": 24, "reason": "stale"},
    )
    [res] = execute_plan([step], ToolContext("s-skip", "t1"))
    assert res.status == "skipped" and "non-returnable" in (res.error or "")
    rows = audit.get_audit("s-skip")
    assert rows[0]["event"] == "skipped" and rows[0]["actor"] == "system"
