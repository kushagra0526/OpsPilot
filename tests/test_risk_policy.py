"""Deterministic risk policy (SPEC §7.1)."""

from __future__ import annotations

from opspilot.agent.schemas import PlanStep
from opspilot.safety.risk import classify


def _step(tool: str, **args) -> PlanStep:
    return PlanStep(id="s1", tool=tool, args=args, rationale="r [db:orders:ORD-1001]")


def test_large_refund_requires_approval() -> None:
    r = classify(_step("issue_refund", order_id="ORD-1001", amount=1299, reason="dead pixel"))
    assert r.level == "high" and r.requires_approval
    assert any("exceeds the $200 auto-refund limit" in x for x in r.reasons)


def test_small_refund_no_approval() -> None:
    r = classify(_step("issue_refund", order_id="ORD-1007", amount=149, reason="uncomfortable"))
    assert r.level == "medium" and not r.requires_approval


def test_refund_exactly_at_limit_is_auto() -> None:
    step = _step("issue_refund", order_id="ORD-1006", amount=200, reason="x")
    assert not classify(step).requires_approval


def test_cancel_requires_approval() -> None:
    r = classify(_step("cancel_order", order_id="ORD-1008", reason="customer request"))
    assert r.level == "high" and r.requires_approval


def test_gold_coupon_over_tier_limit_requires_approval() -> None:
    r = classify(_step("issue_coupon", customer_id="C-001", percent_off=12, reason="goodwill"))
    assert r.level == "high" and r.requires_approval
    assert any("gold-tier limit of 10%" in x for x in r.reasons)


def test_gold_coupon_within_limit_is_auto() -> None:
    r = classify(_step("issue_coupon", customer_id="C-001", percent_off=10, reason="goodwill"))
    assert r.level == "low" and not r.requires_approval


def test_standard_goodwill_coupon_requires_approval() -> None:
    step = _step("issue_coupon", customer_id="C-012", percent_off=5, reason="g")
    assert classify(step).requires_approval


def test_shipping_delay_coupon_for_standard_customer_is_auto() -> None:
    step = _step(
        "issue_coupon",
        customer_id="C-012",
        percent_off=10,
        reason="late",
        purpose="shipping_delay",
        order_id="ORD-1003",
    )
    assert not classify(step).requires_approval


def test_coupon_over_15_always_requires_approval() -> None:
    r = classify(_step("issue_coupon", customer_id="C-007", percent_off=20, reason="g"))
    assert r.requires_approval and any("15% maximum" in x for x in r.reasons)


def test_status_update_to_cancelled_requires_approval() -> None:
    cancel = _step("update_order_status", order_id="ORD-1008", new_status="cancelled")
    ship = _step("update_order_status", order_id="ORD-1008", new_status="shipped")
    assert classify(cancel).requires_approval
    assert not classify(ship).requires_approval


def test_low_confidence_forces_approval() -> None:
    r = classify(_step("create_ticket", category="general", summary="x"), confidence=0.4)
    assert r.requires_approval and r.level == "medium"


def test_read_tools_never_need_approval() -> None:
    r = classify(_step("lookup_order", order_id="ORD-1001"), confidence=0.1)
    assert not r.requires_approval and r.level == "low"


def test_unknown_tool_is_high_risk() -> None:
    r = classify(_step("delete_database"))
    assert r.level == "high" and r.requires_approval
