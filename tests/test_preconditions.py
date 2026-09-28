"""Business-rule preconditions and idempotent refunds (SPEC §7.2, §6.3)."""

from __future__ import annotations

from opspilot import db
from opspilot.safety.preconditions import check
from opspilot.tools.registry import ToolContext, run_tool

CTX = ToolContext(session_id="sess-test", turn_id="t1")


def test_eligible_refunds_pass(fresh_db) -> None:
    assert check("issue_refund", {"order_id": "ORD-1007", "amount": 149, "reason": "x"}).ok
    assert check("issue_refund", {"order_id": "ORD-1001", "amount": 1299, "reason": "x"}).ok
    partial = {"order_id": "ORD-1006", "amount": 89, "reason": "x", "skus": ["AP-JKT-03"]}
    assert check("issue_refund", partial).ok


def test_refund_exceeding_remaining_rejected(fresh_db) -> None:
    res = check("issue_refund", {"order_id": "ORD-1007", "amount": 5000, "reason": "x"})
    assert not res.ok
    assert any("exceeds the refundable remaining $149.00" in r for r in res.reasons)


def test_zero_refund_rejected(fresh_db) -> None:
    assert not check("issue_refund", {"order_id": "ORD-1007", "amount": 0, "reason": "x"}).ok


def test_grocery_refund_fails(fresh_db) -> None:
    res = check("issue_refund", {"order_id": "ORD-1005", "amount": 24, "reason": "stale"})
    assert not res.ok and any("non-returnable" in r for r in res.reasons)


def test_out_of_window_refund_fails(fresh_db) -> None:
    res = check("issue_refund", {"order_id": "ORD-1002", "amount": 89, "reason": "x"})
    assert not res.ok
    assert any("outside the 30-day apparel return window" in r for r in res.reasons)


def test_missing_delivery_date_fails(fresh_db) -> None:
    res = check("issue_refund", {"order_id": "ORD-1004", "amount": 149, "reason": "x"})
    assert not res.ok and any("no delivery date recorded" in r for r in res.reasons)


def test_partial_refund_cannot_exceed_item_value(fresh_db) -> None:
    args = {"order_id": "ORD-1006", "amount": 100, "reason": "x", "skus": ["AP-JKT-03"]}
    assert not check("issue_refund", args).ok


def test_refund_unknown_order(fresh_db) -> None:
    assert not check("issue_refund", {"order_id": "ORD-9999", "amount": 1, "reason": "x"}).ok


def test_cancel_shipped_fails_and_placed_passes(fresh_db) -> None:
    shipped = check("cancel_order", {"order_id": "ORD-1003", "reason": "x"})
    assert not shipped.ok and "only orders not yet shipped" in shipped.reasons[0]
    assert check("cancel_order", {"order_id": "ORD-1008", "reason": "x"}).ok


def test_shipping_delay_coupon_requires_real_delay(fresh_db) -> None:
    base = {"percent_off": 10, "reason": "late", "purpose": "shipping_delay"}
    assert check("issue_coupon", {**base, "customer_id": "C-012", "order_id": "ORD-1003"}).ok
    assert not check("issue_coupon", {**base, "customer_id": "C-007", "order_id": "ORD-1008"}).ok


def test_status_transitions(fresh_db) -> None:
    assert check("update_order_status", {"order_id": "ORD-1008", "new_status": "shipped"}).ok
    assert not check("update_order_status", {"order_id": "ORD-1001", "new_status": "placed"}).ok


def test_invalid_args_and_unknown_tool(fresh_db) -> None:
    assert not check("issue_refund", {"order_id": "1001", "amount": 1, "reason": "x"}).ok
    assert not check("wire_money", {}).ok


def test_duplicate_refund_is_idempotent(fresh_db) -> None:
    args = {"order_id": "ORD-1007", "amount": 149.0, "reason": "uncomfortable"}
    first = run_tool("issue_refund", args, CTX)
    second = run_tool("issue_refund", args, CTX)
    assert first["ok"] and second["ok"] and second["data"]["duplicate"]
    assert first["data"]["refund_id"] == second["data"]["refund_id"]
    assert db.query_one("SELECT COUNT(*) AS n FROM refunds WHERE order_id='ORD-1007'")["n"] == 1
    assert db.query_one("SELECT status FROM orders WHERE id='ORD-1007'")["status"] == "refunded"


def test_store_credit_refund_creates_credit_coupon(fresh_db) -> None:
    args = {
        "order_id": "ORD-1006",
        "amount": 89,
        "reason": "jacket",
        "method": "store_credit",
        "skus": ["AP-JKT-03"],
    }
    res = run_tool("issue_refund", args, CTX)
    assert res["ok"] and res["data"]["order_status"] == "partially_refunded"
    coupon = db.query_one("SELECT * FROM coupons WHERE code=?", (res["data"]["coupon"]["code"],))
    assert coupon["amount_credit"] == 89 and coupon["percent_off"] == 0
    assert coupon["code"].startswith("NOVA-") and len(coupon["code"]) == 11


def test_refund_method_defaults_to_customer_preference(fresh_db) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE customers SET preferred_refund_method='store_credit' WHERE id='C-007'")
    res = run_tool("issue_refund", {"order_id": "ORD-1006", "amount": 89, "reason": "x"}, CTX)
    assert res["data"]["method"] == "store_credit"


def test_ticket_and_escalation_ids(fresh_db) -> None:
    ticket_args = {"order_id": "ORD-1003", "category": "shipping_investigation", "summary": "late"}
    t = run_tool("create_ticket", ticket_args, CTX)
    e = run_tool("escalate_to_human", {"order_id": "ORD-1004", "summary": "missing date"}, CTX)
    assert t["data"]["ticket_id"].startswith("TKT-") and len(t["data"]["ticket_id"]) == 8
    row = db.query_one("SELECT * FROM tickets WHERE id=?", (e["data"]["ticket_id"],))
    assert row["category"] == "escalation" and row["priority"] == "high"


def test_noop_status_change_is_reported_as_no_change(fresh_db) -> None:
    res = check("update_order_status", {"order_id": "ORD-1008", "new_status": "placed"})
    assert not res.ok and res.reasons[0].startswith("no change needed")


def test_escalation_is_allowed_for_unknown_records(fresh_db) -> None:
    # Escalation is how missing or unknown data reaches a person, so it must never be blocked.
    args = {
        "summary": "Customer asks about an order we cannot find",
        "order_id": "ORD-9999",
        "customer_id": "C-001",
    }
    assert check("escalate_to_human", args).ok
    ticket = {"order_id": "ORD-9999", "category": "returns", "summary": "x"}
    assert not check("create_ticket", ticket).ok, "ordinary tickets still need a real order"
