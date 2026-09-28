"""Read helpers over the SQLite transactional data, including computed order fields."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from opspilot import db
from opspilot.agent.schemas import Evidence
from opspilot.config import policy_rules, today


def _days_between(start: str, end: date) -> int:
    return (end - date.fromisoformat(start[:10])).days


def get_order(order_id: str) -> dict[str, Any] | None:
    """Order row + items (with product details) + prior refunds + computed fields."""
    order = db.query_one("SELECT * FROM orders WHERE id=?", (order_id,))
    if order is None:
        return None
    order["items"] = db.query(
        "SELECT i.sku, i.qty, i.unit_price, p.name, p.category FROM order_items i "
        "JOIN products p ON p.sku = i.sku WHERE i.order_id=? ORDER BY i.id",
        (order_id,),
    )
    order["refunds"] = db.query(
        "SELECT id, amount, method, reason, approved_by, created_at FROM refunds "
        "WHERE order_id=? ORDER BY id",
        (order_id,),
    )
    ref = today()
    refunded = round(sum(r["amount"] for r in order["refunds"]), 2)
    promise = date.fromisoformat(order["order_date"]) + timedelta(days=policy_rules().promise_days)
    undelivered = order["status"] in ("placed", "shipped")
    order["computed"] = {
        "days_since_delivery": (
            _days_between(order["delivery_date"], ref) if order["delivery_date"] else None
        ),
        "promise_date": promise.isoformat(),
        "days_past_promise": (ref - promise).days if undelivered else None,
        "refunded_so_far": refunded,
        "refundable_remaining": round(order["total"] - refunded, 2),
    }
    return order


def get_customer(customer_id: str | None = None, email: str | None = None) -> dict[str, Any] | None:
    """Customer row (by ID or email) + their five most recent orders."""
    if customer_id:
        row = db.query_one("SELECT * FROM customers WHERE id=?", (customer_id,))
    elif email:
        row = db.query_one("SELECT * FROM customers WHERE lower(email)=lower(?)", (email,))
    else:
        return None
    if row is None:
        return None
    row["recent_orders"] = db.query(
        "SELECT id, status, order_date, total FROM orders WHERE customer_id=? "
        "ORDER BY order_date DESC LIMIT 5",
        (row["id"],),
    )
    return row


def get_product(sku: str) -> dict[str, Any] | None:
    """Product row."""
    return db.query_one("SELECT * FROM products WHERE sku=?", (sku,))


def find_skus_by_name(text: str) -> list[str]:
    """SKUs whose product name appears in the text (case-insensitive, whole words)."""
    found: list[str] = []
    lowered = text.lower()
    for row in db.query("SELECT sku, name FROM products"):
        if re.search(rf"\b{re.escape(row['name'].lower())}\b", lowered):
            found.append(row["sku"])
    return found


def customer_ids_for_orders(order_ids: list[str]) -> list[str]:
    """Owners of the given orders (unknown orders are ignored)."""
    if not order_ids:
        return []
    marks = ",".join("?" * len(order_ids))
    rows = db.query(f"SELECT DISTINCT customer_id FROM orders WHERE id IN ({marks})", order_ids)
    return [r["customer_id"] for r in rows]


def order_evidence(order: dict[str, Any]) -> Evidence:
    """Render an order (with computed fields) as a citable evidence item."""
    c = order["computed"]
    items = "; ".join(
        f"{i['qty']}x {i['sku']} {i['name']} (category {i['category']}) @ ${i['unit_price']:.2f}"
        for i in order["items"]
    )
    if order["delivery_date"]:
        delivery = f"delivery_date {order['delivery_date']} ({c['days_since_delivery']} days ago)"
    else:
        delivery = "delivery_date NOT RECORDED"
    refunds = (
        "; ".join(
            f"${r['amount']:.2f} via {r['method']} on {r['created_at'][:10]}"
            for r in order["refunds"]
        )
        or "none"
    )
    lines = [
        f"Order {order['id']}: customer {order['customer_id']}, status {order['status']}, "
        f"order_date {order['order_date']}, {delivery}, total ${order['total']:.2f}, "
        f"paid by {order['payment_method']}.",
        f"Items: {items}.",
        f"Prior refunds: {refunds}. Refundable remaining: ${c['refundable_remaining']:.2f}.",
    ]
    if c["days_past_promise"] is not None:
        lines.append(
            f"Not yet delivered. Delivery promise date {c['promise_date']}; "
            f"{c['days_past_promise']} days past promise as of {today().isoformat()}."
        )
    return Evidence(
        id=f"db:orders:{order['id']}",
        source_type="db",
        content=" ".join(lines),
        metadata={"table": "orders", "computed": c, "status": order["status"]},
    )


def customer_evidence(customer: dict[str, Any]) -> Evidence:
    """Render a customer as a citable evidence item."""
    recent = ", ".join(f"{o['id']} ({o['status']})" for o in customer["recent_orders"]) or "none"
    pref = customer["preferred_refund_method"] or "not recorded"
    return Evidence(
        id=f"db:customers:{customer['id']}",
        source_type="db",
        content=(
            f"Customer {customer['id']} {customer['name']} <{customer['email']}>, loyalty tier "
            f"{customer['tier']}, preferred refund method: {pref}. Recent orders: {recent}."
        ),
        metadata={"table": "customers", "tier": customer["tier"]},
    )
