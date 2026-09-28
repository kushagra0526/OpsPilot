"""Deterministic risk policy. `requires_approval` is decided here and nowhere else."""

from __future__ import annotations

from typing import Any

from opspilot import db
from opspilot.agent.schemas import PlanStep, RiskAssessment
from opspilot.config import CONFIDENCE_APPROVAL_THRESHOLD, policy_rules
from opspilot.tools.registry import get_tool

_ORDER = {"low": 0, "medium": 1, "high": 2}


def _raise(level: str, to: str) -> str:
    return to if _ORDER[to] > _ORDER[level] else level


def _customer_tier(customer_id: str | None) -> str | None:
    if not customer_id:
        return None
    row = db.query_one("SELECT tier FROM customers WHERE id=?", (customer_id,))
    return row["tier"] if row else None


def _coupon_rules(args: dict[str, Any]) -> tuple[bool, list[str]]:
    percent = int(args.get("percent_off", 0))
    rules = policy_rules()
    reasons: list[str] = []
    if percent > rules.max_coupon_percent:
        reasons.append(
            f"coupon {percent}% exceeds the {rules.max_coupon_percent}% maximum without manager approval (Loyalty Program §4)"
        )
    if args.get("purpose") == "shipping_delay":
        if percent > rules.delay_coupon_percent:
            reasons.append(
                f"shipping-delay coupon {percent}% exceeds the {rules.delay_coupon_percent}% set by Shipping Policy §3"
            )
    else:
        tier = _customer_tier(args.get("customer_id")) or "standard"
        limit = rules.goodwill_limits.get(tier, 0)
        if percent > limit:
            reasons.append(
                f"goodwill coupon {percent}% exceeds the {tier}-tier limit of {limit}% (Loyalty Program §3)"
            )
    return bool(reasons), reasons


def classify(step: PlanStep, confidence: float = 1.0) -> RiskAssessment:
    """Classify one step. Unknown tools are high risk and always need approval."""
    spec = get_tool(step.tool)
    if spec is None:
        return RiskAssessment(
            level="high", requires_approval=True, reasons=[f"unknown tool {step.tool}"]
        )
    if spec.kind == "read":
        return RiskAssessment(level="low", requires_approval=False, reasons=["read-only lookup"])
    level: str = spec.base_risk
    approval = False
    reasons: list[str] = []
    args = step.args
    if step.tool == "issue_refund":
        amount = float(args.get("amount", 0) or 0)
        limit = float(policy_rules().auto_refund_limit)
        if amount > limit:
            level, approval = "high", True
            reasons.append(
                f"${amount:,.2f} exceeds the ${limit:.0f} auto-refund limit (Refund Policy §3)"
            )
        else:
            level = "medium"
            reasons.append(
                f"${amount:,.2f} is within the ${limit:.0f} auto-refund limit (Refund Policy §3)"
            )
        reasons.append("refunds are irreversible")
    elif step.tool == "cancel_order":
        level, approval = "high", True
        reasons.append(
            "cancellation is irreversible and needs supervisor approval (Shipping Policy §4)"
        )
    elif step.tool == "issue_coupon":
        needs, why = _coupon_rules(args)
        if needs:
            level, approval = "high", True
            reasons.extend(why)
    elif step.tool == "update_order_status" and args.get("new_status") in ("cancelled", "refunded"):
        level, approval = "high", True
        reasons.append(f"status change to {args.get('new_status')} is effectively irreversible")
    if confidence < CONFIDENCE_APPROVAL_THRESHOLD:
        level, approval = _raise(level, "medium"), True
        reasons.append(
            f"agent confidence {confidence:.2f} is below {CONFIDENCE_APPROVAL_THRESHOLD}"
        )
    if not reasons:
        reasons.append(
            f"base risk {spec.base_risk}; {'reversible' if spec.reversible else 'not reversible'}"
        )
    return RiskAssessment(level=level, requires_approval=approval, reasons=reasons)  # type: ignore[arg-type]
