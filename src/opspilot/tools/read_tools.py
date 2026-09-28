"""Read-only tools (low risk, never need approval) used by the replan loop to fetch more data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from opspilot.knowledge import faq, inventory, retriever, structured, vector_store
from opspilot.tools.registry import ToolContext, ToolSpec, register

ORDER_ID = r"^ORD-\d{4}$"


class LookupOrderArgs(BaseModel):
    """Args for lookup_order."""

    order_id: str = Field(pattern=ORDER_ID)


class LookupCustomerArgs(BaseModel):
    """Args for lookup_customer (one of customer_id or email)."""

    customer_id: str | None = Field(default=None, pattern=r"^C-\d{3}$")
    email: str | None = None

    @model_validator(mode="after")
    def _one_key(self) -> LookupCustomerArgs:
        if not (self.customer_id or self.email):
            raise ValueError("customer_id or email is required")
        return self


class CheckInventoryArgs(BaseModel):
    """Args for check_inventory."""

    sku: str = Field(pattern=r"^[A-Z]{2}-[A-Z]{3}-\d{2}$")


class SearchArgs(BaseModel):
    """Args for search_policies / search_faq."""

    query: str = Field(min_length=3)


def _result(ok: bool, summary: str, evidence: list[Any], **data: Any) -> dict[str, Any]:
    return {
        "ok": ok,
        "summary": summary,
        "data": data,
        "evidence": [e.model_dump() for e in evidence],
    }


def lookup_order(args: LookupOrderArgs, ctx: ToolContext) -> dict[str, Any]:
    """Fetch an order with items, refunds and computed fields."""
    order = structured.get_order(args.order_id)
    if order is None:
        return _result(False, f"Order {args.order_id} not found", [])
    ev = structured.order_evidence(order)
    return _result(True, ev.content, [ev], order_id=order["id"], status=order["status"])


def lookup_customer(args: LookupCustomerArgs, ctx: ToolContext) -> dict[str, Any]:
    """Fetch a customer by ID or email."""
    cust = structured.get_customer(customer_id=args.customer_id, email=args.email)
    if cust is None:
        return _result(False, f"Customer {args.customer_id or args.email} not found", [])
    ev = structured.customer_evidence(cust)
    return _result(True, ev.content, [ev], customer_id=cust["id"], tier=cust["tier"])


def check_inventory(args: CheckInventoryArgs, ctx: ToolContext) -> dict[str, Any]:
    """Merged DB + CSV stock view with conflict resolution."""
    view = inventory.get_stock(args.sku)
    if view is None:
        return _result(False, f"SKU {args.sku} not found", [])
    note = view["conflict"]
    summary = (
        f"{args.sku}: resolved stock {view['resolved_stock']} (source {view['resolved_source']})"
    )
    if note:
        summary += f". Conflict: {note.detail} {note.resolution}"
    return _result(
        True,
        summary,
        inventory.stock_evidence(view),
        sku=args.sku,
        resolved_stock=view["resolved_stock"],
        conflict=note.model_dump() if note else None,
    )


def search_policies(args: SearchArgs, ctx: ToolContext) -> dict[str, Any]:
    """Semantic policy search (superseded chunks are labelled)."""
    hits = vector_store.search_policies([args.query], 4)
    evidence, conflicts = retriever.apply_versions(hits, [args.query])
    summary = "; ".join(
        e.metadata["label"] + (" (SUPERSEDED)" if e.superseded else "") for e in evidence
    )
    return _result(
        True, f"Found: {summary}", evidence, conflicts=[c.model_dump() for c in conflicts]
    )


def search_faq(args: SearchArgs, ctx: ToolContext) -> dict[str, Any]:
    """Semantic FAQ search."""
    evidence = [faq.to_evidence(h) for h in faq.search([args.query])]
    return _result(True, f"Found {len(evidence)} FAQ entries", evidence)


for _name, _desc, _model, _fn in [
    (
        "lookup_order",
        "Fetch an order with items, prior refunds and computed fields.",
        LookupOrderArgs,
        lookup_order,
    ),
    (
        "lookup_customer",
        "Fetch a customer profile by customer_id or email.",
        LookupCustomerArgs,
        lookup_customer,
    ),
    (
        "check_inventory",
        "Check stock for a SKU across the DB and the warehouse CSV feed.",
        CheckInventoryArgs,
        check_inventory,
    ),
    ("search_policies", "Search NovaMart policy documents.", SearchArgs, search_policies),
    ("search_faq", "Search the NovaMart FAQ.", SearchArgs, search_faq),
]:
    register(ToolSpec(_name, _desc, _model, _fn, kind="read", base_risk="low", reversible=True))
