"""Business-rule checks run before approval and again right before execution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from opspilot import db
from opspilot.config import policy_rules
from opspilot.knowledge.structured import get_order
from opspilot.tools.action_tools import TRANSITIONS
from opspilot.tools.registry import get_tool, validate_args


class PreconditionResult(BaseModel):
    """Whether a step may run, and why not."""

    ok: bool
    reasons: list[str] = []


def _customer_exists(customer_id: str | None) -> bool:
    return (
        bool(customer_id)
        and db.query_one("SELECT 1 AS x FROM customers WHERE id=?", (customer_id,)) is not None
    )


def _item_eligibility(order: dict[str, Any], skus: list[str]) -> list[str]:
    """Return-window and non-returnable checks under the current policy (Return Policy v2)."""
    problems: list[str] = []
    items = [i for i in order["items"] if not skus or i["sku"] in skus]
    days = order["computed"]["days_since_delivery"]
    for item in items:
        window = policy_rules().return_windows.get(item["category"])
        if window is None:
            problems.append(
                f"{item['sku']} {item['name']} is {item['category']}: non-returnable (Return Policy v2 §3)"
            )
        elif days is not None and days > window:
            problems.append(
                f"{item['sku']} {item['name']} was delivered {days} days ago, outside the "
                f"{window}-day {item['category']} return window (Return Policy v2 §2)"
            )
    return problems


def _refund(args: Any) -> list[str]:
    order = get_order(args.order_id)
    if order is None:
        return [f"order {args.order_id} not found"]
    errs: list[str] = []
    if not _customer_exists(order["customer_id"]):
        errs.append(f"customer {order['customer_id']} not found")
    if order["status"] not in ("delivered", "partially_refunded"):
        errs.append(f"order status is {order['status']}; only delivered orders can be refunded")
    remaining = order["computed"]["refundable_remaining"]
    if args.amount <= 0:
        errs.append("refund amount must be greater than $0")
    elif args.amount > remaining + 1e-9:
        errs.append(
            f"requested ${args.amount:,.2f} exceeds the refundable remaining ${remaining:,.2f} "
            "(order total minus prior refunds, Refund Policy §4)"
        )
    unknown = [s for s in args.skus if s not in {i["sku"] for i in order["items"]}]
    if unknown:
        errs.append(f"SKUs {unknown} are not part of {args.order_id}")
    elif args.skus:
        value = sum(i["qty"] * i["unit_price"] for i in order["items"] if i["sku"] in args.skus)
        if args.amount > value + 1e-9:
            errs.append(
                f"partial refund ${args.amount:,.2f} exceeds the price of the returned items ${value:,.2f}"
            )
    if order["status"] == "delivered" and not order["delivery_date"]:
        errs.append(
            f"{args.order_id} has no delivery date recorded, so return eligibility cannot be "
            "determined; escalate instead (Return Policy v2 §5)"
        )
    else:
        errs.extend(_item_eligibility(order, args.skus))
    return errs


def _cancel(args: Any) -> list[str]:
    order = db.query_one("SELECT status FROM orders WHERE id=?", (args.order_id,))
    if order is None:
        return [f"order {args.order_id} not found"]
    if order["status"] != "placed":
        return [
            f"order is {order['status']}; only orders not yet shipped (placed) can be cancelled (Shipping Policy §4)"
        ]
    return []


def _coupon(args: Any) -> list[str]:
    errs: list[str] = []
    if not _customer_exists(args.customer_id):
        errs.append(f"customer {args.customer_id} not found")
    if not 0 < args.percent_off <= 100:
        errs.append("percent_off must be between 1 and 100")
    if args.purpose == "shipping_delay":
        order = get_order(args.order_id) if args.order_id else None
        if order is None:
            errs.append("a shipping-delay coupon needs a valid order_id")
        else:
            late = order["computed"]["days_past_promise"]
            if order["customer_id"] != args.customer_id:
                errs.append(f"{args.order_id} does not belong to {args.customer_id}")
            threshold = policy_rules().delay_threshold_days
            if late is None or late <= threshold:
                errs.append(
                    f"{args.order_id} is not more than {threshold} days past its "
                    "delivery promise (Shipping Policy §3)"
                )
    return errs


def _refs(args: Any) -> list[str]:
    errs: list[str] = []
    if getattr(args, "customer_id", None) and not _customer_exists(args.customer_id):
        errs.append(f"customer {args.customer_id} not found")
    order_id = getattr(args, "order_id", None)
    if order_id:
        order = db.query_one("SELECT customer_id FROM orders WHERE id=?", (order_id,))
        if order is None:
            errs.append(f"order {order_id} not found")
        elif getattr(args, "customer_id", None) and order["customer_id"] != args.customer_id:
            errs.append(f"{order_id} does not belong to {args.customer_id}")
    return errs


def _status(args: Any) -> list[str]:
    order = db.query_one("SELECT status FROM orders WHERE id=?", (args.order_id,))
    if order is None:
        return [f"order {args.order_id} not found"]
    if args.new_status == order["status"]:
        return [f"no change needed: {args.order_id} is already {order['status']}"]
    if args.new_status not in TRANSITIONS[order["status"]]:
        return [f"invalid status transition {order['status']} -> {args.new_status}"]
    return []


CHECKS: dict[str, Callable[[Any], list[str]]] = {
    "issue_refund": _refund,
    "cancel_order": _cancel,
    "issue_coupon": _coupon,
    "create_ticket": _refs,
    # No existence checks: escalation is how unknown or missing data reaches a person.
    "escalate_to_human": lambda args: [],
    "send_customer_email": _refs,
    "update_order_status": _status,
}


def check(tool: str, args: dict[str, Any]) -> PreconditionResult:
    """Validate args and business rules for one step."""
    if get_tool(tool) is None:
        return PreconditionResult(ok=False, reasons=[f"unknown tool {tool}"])
    try:
        parsed = validate_args(tool, args)
    except ValueError as exc:
        return PreconditionResult(ok=False, reasons=[str(exc)])
    fn = CHECKS.get(tool)
    reasons = fn(parsed) if fn else []
    return PreconditionResult(ok=not reasons, reasons=reasons)
