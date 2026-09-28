"""Action tools. All write to SQLite and return {"ok": bool, "summary": str, "data": {...}}."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import string
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from opspilot import db
from opspilot.config import policy_rules, today
from opspilot.tools.registry import ToolContext, ToolSpec, register

ORDER_ID = r"^ORD-\d{4}$"
CUSTOMER_ID = r"^C-\d{3}$"
OrderStatus = Literal[
    "placed", "shipped", "delivered", "cancelled", "refunded", "partially_refunded"
]

# Allowed status transitions for update_order_status (and used by preconditions).
TRANSITIONS: dict[str, set[str]] = {
    "placed": {"shipped", "cancelled"},
    "shipped": {"delivered"},
    "delivered": {"refunded", "partially_refunded"},
    "partially_refunded": {"refunded"},
    "cancelled": set(),
    "refunded": set(),
}


class IssueRefundArgs(BaseModel):
    """Args for issue_refund."""

    order_id: str = Field(pattern=ORDER_ID)
    amount: float
    reason: str
    method: Literal["original", "store_credit"] | None = Field(
        default=None,
        description="'original' payment method or 'store_credit'. Omit to use the customer's recorded preference.",
    )
    skus: list[str] = Field(
        default_factory=list,
        description="SKUs being refunded for a partial refund; empty = whole order.",
    )


class CancelOrderArgs(BaseModel):
    """Args for cancel_order."""

    order_id: str = Field(pattern=ORDER_ID)
    reason: str


class IssueCouponArgs(BaseModel):
    """Args for issue_coupon."""

    customer_id: str = Field(pattern=CUSTOMER_ID)
    percent_off: int
    reason: str
    purpose: Literal["goodwill", "shipping_delay"] = Field(
        default="goodwill",
        description="'goodwill' (loyalty_program §3 tier limits) or 'shipping_delay' (shipping_policy §3, needs order_id).",
    )
    order_id: str | None = Field(default=None, pattern=ORDER_ID)


class CreateTicketArgs(BaseModel):
    """Args for create_ticket."""

    customer_id: str | None = Field(default=None, pattern=CUSTOMER_ID)
    order_id: str | None = Field(default=None, pattern=ORDER_ID)
    category: str = Field(description="e.g. shipping_investigation, returns, billing, general")
    priority: Literal["low", "medium", "high"] = "medium"
    summary: str


class EscalateArgs(BaseModel):
    """Args for escalate_to_human."""

    summary: str
    priority: Literal["low", "medium", "high"] = "high"
    customer_id: str | None = Field(default=None, pattern=CUSTOMER_ID)
    order_id: str | None = Field(default=None, pattern=ORDER_ID)


class SendEmailArgs(BaseModel):
    """Args for send_customer_email."""

    customer_id: str = Field(pattern=CUSTOMER_ID)
    subject: str
    body: str


class UpdateOrderStatusArgs(BaseModel):
    """Args for update_order_status."""

    order_id: str = Field(pattern=ORDER_ID)
    new_status: OrderStatus


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _code(prefix: str, n: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(n))


def _ok(summary: str, **data: Any) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data}


def _fail(summary: str, **data: Any) -> dict[str, Any]:
    return {"ok": False, "summary": summary, "data": data}


def refund_idempotency_key(session_id: str, order_id: str, amount: float) -> str:
    """sha256(session_id + order_id + amount): the same refund request never runs twice."""
    return hashlib.sha256(f"{session_id}{order_id}{amount:.2f}".encode()).hexdigest()


def resolve_refund_method(order_id: str, method: str | None) -> str:
    """Explicit method wins; otherwise the customer's recorded preference; otherwise 'original'."""
    if method:
        return method
    row = db.query_one(
        "SELECT c.preferred_refund_method FROM orders o JOIN customers c ON c.id=o.customer_id "
        "WHERE o.id=?",
        (order_id,),
    )
    return (row or {}).get("preferred_refund_method") or "original"


def _insert_coupon(
    conn: sqlite3.Connection,
    customer_id: str,
    percent: int,
    reason: str,
    credit: float | None = None,
) -> dict[str, Any]:
    expires = (today() + timedelta(days=policy_rules().coupon_validity_days)).isoformat()
    for _ in range(10):
        code = _code("NOVA-", 6)
        try:
            conn.execute(
                "INSERT INTO coupons(code, customer_id, percent_off, amount_credit, reason,"
                " expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
                (code, customer_id, percent, credit, reason, expires, _now()),
            )
            return {
                "code": code,
                "percent_off": percent,
                "amount_credit": credit,
                "expires_at": expires,
            }
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not generate a unique coupon code")


def issue_refund(args: IssueRefundArgs, ctx: ToolContext) -> dict[str, Any]:
    """Refund to the original method or as store credit; idempotent per session/order/amount."""
    key = refund_idempotency_key(ctx.session_id, args.order_id, args.amount)
    existing = db.query_one("SELECT * FROM refunds WHERE idempotency_key=?", (key,))
    if existing:
        return _ok(
            f"Duplicate request: refund #{existing['id']} of ${existing['amount']:.2f} was already issued",
            refund_id=existing["id"],
            amount=existing["amount"],
            method=existing["method"],
            duplicate=True,
        )
    method = resolve_refund_method(args.order_id, args.method)
    with db.transaction() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id=?", (args.order_id,)).fetchone()
        if order is None:
            return _fail(f"Order {args.order_id} not found")
        prior = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM refunds WHERE order_id=?", (args.order_id,)
        ).fetchone()[0]
        remaining = round(order["total"] - prior, 2)
        if args.amount <= 0 or args.amount > remaining + 1e-9:
            return _fail(f"Amount ${args.amount:.2f} exceeds refundable remaining ${remaining:.2f}")
        cur = conn.execute(
            "INSERT INTO refunds(order_id, amount, method, reason, approved_by, created_at,"
            " idempotency_key) VALUES (?,?,?,?,?,?,?)",
            (args.order_id, args.amount, method, args.reason, ctx.approved_by, _now(), key),
        )
        coupon = None
        if method == "store_credit":
            coupon = _insert_coupon(
                conn,
                order["customer_id"],
                0,
                f"Store credit refund for {args.order_id}",
                args.amount,
            )
        new_status = "refunded" if abs(remaining - args.amount) < 1e-9 else "partially_refunded"
        conn.execute("UPDATE orders SET status=? WHERE id=?", (new_status, args.order_id))
    how = (
        f"as store credit (coupon {coupon['code']})" if coupon else "to the original payment method"
    )
    return _ok(
        f"Refunded ${args.amount:.2f} for {args.order_id} {how}; order is now {new_status}",
        refund_id=cur.lastrowid,
        amount=args.amount,
        method=method,
        order_status=new_status,
        coupon=coupon,
        approved_by=ctx.approved_by,
    )


def cancel_order(args: CancelOrderArgs, ctx: ToolContext) -> dict[str, Any]:
    """Cancel an order that has not shipped (irreversible)."""
    with db.transaction() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id=?", (args.order_id,)).fetchone()
        if row is None:
            return _fail(f"Order {args.order_id} not found")
        if row["status"] != "placed":
            return _fail(
                f"Order {args.order_id} is {row['status']}; only placed orders can be cancelled"
            )
        conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (args.order_id,))
    return _ok(f"Cancelled {args.order_id}", order_id=args.order_id, order_status="cancelled")


def issue_coupon(args: IssueCouponArgs, ctx: ToolContext) -> dict[str, Any]:
    """Create a NOVA-XXXXXX percentage coupon valid for 90 days."""
    with db.transaction() as conn:
        coupon = _insert_coupon(conn, args.customer_id, args.percent_off, args.reason)
    return _ok(
        f"Issued {args.percent_off}% coupon {coupon['code']} to {args.customer_id} "
        f"(expires {coupon['expires_at']})",
        **coupon,
        customer_id=args.customer_id,
    )


def _insert_ticket(
    customer_id: str | None, order_id: str | None, category: str, priority: str, summary: str
) -> str:
    for _ in range(10):
        tid = _code("TKT-", 4)
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO tickets(id, customer_id, order_id, category, priority, summary,"
                    " status, created_at) VALUES (?,?,?,?,?,?, 'open', ?)",
                    (tid, customer_id, order_id, category, priority, summary, _now()),
                )
            return tid
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not generate a unique ticket id")


def create_ticket(args: CreateTicketArgs, ctx: ToolContext) -> dict[str, Any]:
    """Open a support ticket."""
    tid = _insert_ticket(
        args.customer_id, args.order_id, args.category, args.priority, args.summary
    )
    return _ok(
        f"Opened {args.priority}-priority {args.category} ticket {tid}",
        ticket_id=tid,
        category=args.category,
        priority=args.priority,
    )


def escalate_to_human(args: EscalateArgs, ctx: ToolContext) -> dict[str, Any]:
    """Hand the case to a human via an escalation ticket."""
    tid = _insert_ticket(args.customer_id, args.order_id, "escalation", args.priority, args.summary)
    return _ok(
        f"Escalated to a human agent: {args.priority}-priority ticket {tid}",
        ticket_id=tid,
        category="escalation",
        priority=args.priority,
    )


def send_customer_email(args: SendEmailArgs, ctx: ToolContext) -> dict[str, Any]:
    """Queue an email in the outbox (mock sender)."""
    cust = db.query_one("SELECT email FROM customers WHERE id=?", (args.customer_id,))
    if cust is None:
        return _fail(f"Customer {args.customer_id} not found")
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO outbox(customer_id, to_email, subject, body, created_at) VALUES (?,?,?,?,?)",
            (args.customer_id, cust["email"], args.subject, args.body, _now()),
        )
    return _ok(
        f"Emailed {cust['email']}: {args.subject!r}",
        outbox_id=cur.lastrowid,
        to_email=cust["email"],
    )


def update_order_status(args: UpdateOrderStatusArgs, ctx: ToolContext) -> dict[str, Any]:
    """Change order status along a validated transition."""
    with db.transaction() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id=?", (args.order_id,)).fetchone()
        if row is None:
            return _fail(f"Order {args.order_id} not found")
        if args.new_status not in TRANSITIONS[row["status"]]:
            return _fail(f"Invalid transition {row['status']} -> {args.new_status}")
        conn.execute("UPDATE orders SET status=? WHERE id=?", (args.new_status, args.order_id))
    return _ok(
        f"{args.order_id}: {row['status']} -> {args.new_status}", order_status=args.new_status
    )


for _spec in [
    ToolSpec(
        "issue_refund",
        "Refund an order (full or partial) to the original payment method or as store credit. Also sets the order status to refunded/partially_refunded.",
        IssueRefundArgs,
        issue_refund,
        "action",
        "medium",
        False,
    ),
    ToolSpec(
        "cancel_order",
        "Cancel an order that has not shipped yet (also sets status cancelled). Irreversible.",
        CancelOrderArgs,
        cancel_order,
        "action",
        "high",
        False,
    ),
    ToolSpec(
        "issue_coupon",
        "Issue a percentage-off coupon (goodwill or shipping-delay apology).",
        IssueCouponArgs,
        issue_coupon,
        "action",
        "low",
        True,
    ),
    ToolSpec(
        "create_ticket",
        "Open a support ticket, e.g. a shipping investigation.",
        CreateTicketArgs,
        create_ticket,
        "action",
        "low",
        True,
    ),
    ToolSpec(
        "escalate_to_human",
        "Escalate the case to a human agent (creates a high-priority escalation ticket).",
        EscalateArgs,
        escalate_to_human,
        "action",
        "low",
        True,
    ),
    ToolSpec(
        "send_customer_email",
        "Email the customer (queued in the outbox).",
        SendEmailArgs,
        send_customer_email,
        "action",
        "low",
        False,
    ),
    ToolSpec(
        "update_order_status",
        "Move an order to a new status along valid transitions only. Only for explicit status corrections: never add it after a refund or cancellation, which update the status themselves.",
        UpdateOrderStatusArgs,
        update_order_status,
        "action",
        "medium",
        True,
    ),
]:
    register(_spec)
