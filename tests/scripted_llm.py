"""Offline test double for the 12 example scenarios (tests only; never used by the app).

It reads the same prompts a real model gets and derives its plan from the EVIDENCE block, so all
citations are real evidence IDs and every guardrail (risk, approvals, preconditions, grounding)
runs exactly as it does with the live model. Tests use it to drive whole scenarios and the UI
without network access.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from opspilot.config import policy_rules
from opspilot.llm import _CallLog


@dataclass
class _Order:
    id: str
    customer_id: str
    status: str
    days: int | None
    total: float
    items: list[tuple[str, str, str, float]] = field(
        default_factory=list
    )  # sku, name, category, price
    days_past_promise: int | None = None


@dataclass
class _Prompt:
    request: str
    iteration: int
    ids: list[str]
    lines: dict[str, str]
    attempts: list[dict[str, Any]]

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self.lines

    def pick(self, *candidates: str) -> list[str]:
        found = [c for c in candidates if self.has(c)]
        if found:
            return found
        fallback = [
            i for i in self.ids if i.startswith("policy:") and "SUPERSEDED" not in self.lines[i]
        ]
        return fallback[:1] or self.ids[:1]

    def orders(self) -> list[_Order]:
        result = []
        for eid, text in self.lines.items():
            if not eid.startswith("db:orders:"):
                continue
            m = re.search(r"Order (ORD-\d{4}): customer (C-\d{3}), status (\w+)", text)
            if not m:
                continue
            days = re.search(r"\((\d+) days ago\)", text)
            total = re.search(r"total \$(\d+\.\d{2})", text)
            late = re.search(r"(\d+) days past promise", text)
            items = [
                (s, n.strip(), c, float(p))
                for s, n, c, p in re.findall(
                    r"\dx (\S+) (.+?) \(category (\w+)\) @ \$(\d+\.\d{2})", text
                )
            ]
            result.append(
                _Order(
                    m.group(1),
                    m.group(2),
                    m.group(3),
                    int(days.group(1)) if days else None,
                    float(total.group(1)) if total else 0.0,
                    items,
                    int(late.group(1)) if late else None,
                )
            )
        return result

    def tier(self, customer_id: str) -> str:
        m = re.search(r"loyalty tier (\w+)", self.lines.get(f"db:customers:{customer_id}", ""))
        return m.group(1) if m else "standard"

    def store_credit_memory(self) -> str | None:
        for eid, text in self.lines.items():
            if eid.startswith("memory:") and "store credit" in text.lower():
                return eid
        return None


def _parse(user: str) -> _Prompt:
    request = re.search(r"^REQUEST: (.*)$", user, re.MULTILINE)
    iteration = re.search(r"^ITERATION: (\d+)", user, re.MULTILINE)
    evidence = user.split("## EVIDENCE", 1)[-1].split("\n## ", 1)[0]
    lines = {
        m.group(1): m.group(0) for m in re.finditer(r"^\[([^\]]+)\] \(.*$", evidence, re.MULTILINE)
    }
    attempts: list[dict[str, Any]] = []
    if "## PREVIOUS ATTEMPTS" in user:
        block = user.split("## PREVIOUS ATTEMPTS", 1)[1].split("\n## ", 1)[0]
        attempts = [json.loads(line) for line in block.splitlines() if line.startswith("{")]
    return _Prompt(
        request.group(1) if request else "",
        int(iteration.group(1)) if iteration else 1,
        list(lines),
        lines,
        attempts,
    )


def _step(
    sid: str,
    tool: str,
    args: dict[str, Any],
    cites: list[str],
    why: str,
    deps: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "tool": tool,
        "args": args,
        "depends_on": deps or [],
        "rationale": f"{why} [{', '.join(cites)}]",
        "citations": cites,
        "expected_outcome": f"{tool} completes",
    }


def _decision(
    kind: str,
    analysis: str,
    steps: list[dict[str, Any]] | None = None,
    answer: str | None = None,
    conflicts: list[str] | None = None,
    missing: list[str] | None = None,
    confidence: float = 0.9,
) -> str:
    return json.dumps(
        {
            "decision_type": kind,
            "analysis": analysis,
            "conflicts_considered": conflicts or [],
            "missing_information": missing or [],
            "steps": steps or [],
            "answer_draft": answer,
            "confidence": confidence,
        }
    )


def _email(
    sid: str, customer_id: str, subject: str, body: str, cites: list[str], deps: list[str]
) -> dict[str, Any]:
    return _step(
        sid,
        "send_customer_email",
        {"customer_id": customer_id, "subject": subject, "body": body},
        cites,
        "Confirm the outcome to the customer by email after the action completes",
        deps,
    )


class ScriptedScenarioLLM:
    """Scripted stand-in for the live models, used only by tests."""

    def __init__(self) -> None:
        self._log = _CallLog()

    def describe(self) -> dict[str, str]:
        """Provider/model info for the UI."""
        return {
            "provider": "offline test double",
            "large": "scripted-planner",
            "small": "scripted-extractor",
        }

    def drain_calls(self) -> list[dict[str, Any]]:
        """Return and clear per-call records."""
        return self._log.drain()

    def complete(
        self, node: str, system: str, user: str, *, tier: str, temperature: float, json_mode: bool
    ) -> str:
        """Dispatch by node name."""
        handler = {"understand": self._understand, "plan": self._plan, "memory": self._memory}.get(
            node
        )
        text = handler(user) if handler else ""
        self._log.add(
            {
                "node": node,
                "event": "call",
                "provider": "offline test double",
                "tier": tier,
                "input_tokens": len(system + user) // 4,
                "output_tokens": len(text) // 4,
                "retries": 0,
                "latency": 0.0,
            }
        )
        return text

    # ---- nodes ---------------------------------------------------------------------------
    @staticmethod
    def _understand(user: str) -> str:
        text = user.removeprefix("REQUEST: ").strip()
        low = text.lower()
        if any(w in low for w in ("weather", "cricket", "stock market", "recipe")):
            kind = "out_of_scope"
        elif any(
            w in low
            for w in ("refund", "return", "cancel", "email", "note", "exchange", "where is")
        ):
            kind = "mixed" if "?" in text else "action"
        else:
            kind = "question"
        return json.dumps({"summary": text, "request_type": kind})

    @staticmethod
    def _memory(user: str) -> str:
        req = re.search(r"OPERATOR REQUEST: (.*)", user)
        text = req.group(1) if req else ""
        cust = re.search(r"C-\d{3}", text)
        if cust and "prefer" in text.lower() and "store credit" in text.lower():
            return json.dumps(
                {
                    "memories": [
                        {
                            "subject_type": "customer",
                            "subject_id": cust.group(0),
                            "kind": "preference",
                            "content": "Prefers store credit over card refunds",
                            "refund_method": "store_credit",
                        }
                    ],
                    "interaction_summary": f"Operator recorded that {cust.group(0)} prefers store credit.",
                }
            )
        return json.dumps({"memories": [], "interaction_summary": ""})

    def _plan(self, user: str) -> str:
        p = _parse(user)
        low = p.request.lower()
        if "Signal: likely unrelated" in user:
            return _decision(
                "refuse",
                "The request is not about NovaMart and matched no internal record.",
                answer="I can only help with NovaMart support questions, using NovaMart's internal knowledge.",
                confidence=0.95,
            )
        orders = p.orders()
        if "return window" in low:
            return self._window_answer(p)
        if "prefer" in low and "store credit" in low:
            return self._preference_ack(p)
        if any(w in low for w in ("ignore all", "authorized", "authorised")) and orders:
            return self._injection(p, orders[0])
        if ("exchange" in low or "in stock" in low) and orders:
            return self._exchange(p, orders[0])
        if "cancel" in low and orders:
            return self._cancel(p, orders[0])
        if (
            orders
            and orders[0].status in ("placed", "shipped")
            and orders[0].days_past_promise is not None
        ):
            return self._late(p, orders[0])
        if orders and any(w in low for w in ("refund", "return")):
            return self._refund(p, orders[0])
        return _decision(
            "clarify",
            "The offline test double only scripts the example scenarios.",
            answer="The offline test double has no script for this request.",
            confidence=0.3,
        )

    # ---- scenarios -----------------------------------------------------------------------
    def _window_answer(self, p: _Prompt) -> str:
        v2 = p.pick("policy:return_policy_v2§2")
        v1 = [i for i in p.ids if i.startswith("policy:return_policy_v1")][:1]
        prod = p.pick("db:products:EL-HP-02")
        old = (
            f" Return Policy v1 [{v1[0]}] allowed 30 days for everything, but it is superseded by v2."
            if v1
            else ""
        )
        analysis = (
            f"Nova Buds Pro (EL-HP-02) is in the electronics category [{prod[0]}]. The current "
            f"Return Policy v2 sets a 14-day window from delivery for electronics [{v2[0]}].{old}"
        )
        return _decision(
            "answer",
            analysis,
            answer=f"Nova Buds Pro can be returned within 14 days of delivery [{v2[0]}]. "
            "The older 30-day rule (v1) no longer applies.",
            conflicts=["return_policy_v1 superseded by return_policy_v2; applied v2"],
        )

    def _preference_ack(self, p: _Prompt) -> str:
        cust = next((i for i in p.ids if i.startswith("db:customers:")), None)
        method = p.pick("policy:refund_policy§2")
        cites = f"[{cust}] [{method[0]}]" if cust else f"[{method[0]}]"
        return _decision(
            "answer",
            f"The operator reports a refund-method preference for this customer {cites}. "
            "Refund Policy allows store credit when the customer prefers it.",
            answer=f"Noted. Future refunds for this customer will default to store credit {cites}.",
        )

    def _refund(self, p: _Prompt, o: _Order) -> str:
        cites_o = f"db:orders:{o.id}"
        missing_sop = p.pick("policy:escalation_sop§2", "policy:return_policy_v2§5")
        if o.days is None and o.status == "delivered":
            return _decision(
                "escalate",
                f"{o.id} is marked delivered but has no delivery date [{cites_o}], so the return "
                f"window cannot be computed; policy requires escalation [{', '.join(missing_sop)}].",
                [
                    _step(
                        "s1",
                        "escalate_to_human",
                        {
                            "summary": f"Refund request for {o.id}: delivery date missing, eligibility "
                            "cannot be determined.",
                            "priority": "high",
                            "customer_id": o.customer_id,
                            "order_id": o.id,
                        },
                        [cites_o, *missing_sop],
                        "Missing delivery date means eligibility "
                        "cannot be determined; escalate instead of refunding",
                    )
                ],
                missing=[f"{o.id} delivery_date"],
            )
        wanted = [it for it in o.items if it[1].lower().split()[-1] in p.request.lower()] or o.items
        cats = {it[2] for it in wanted}
        v2_2, v2_3 = p.pick("policy:return_policy_v2§2"), p.pick("policy:return_policy_v2§3")
        if "grocery" in cats:
            return _decision(
                "answer",
                f"{o.id} contains grocery items [{cites_o}]; grocery and perishables are "
                f"non-returnable under the current policy [{v2_3[0]}].",
                answer=f"The refund is denied: grocery items are non-returnable [{v2_3[0]}]. "
                "If the customer disputes this, I can escalate to a specialist.",
            )
        window = min(policy_rules().return_windows[c] or 0 for c in cats)
        if o.days is not None and o.days > window:
            return self._goodwill(p, o, window, v2_2)
        amount = round(sum(it[3] for it in wanted), 2)
        skus = [it[0] for it in wanted] if len(wanted) < len(o.items) else []
        mem = p.store_credit_memory()
        args: dict[str, Any] = {"order_id": o.id, "amount": amount, "reason": p.request[:120]}
        if skus:
            args["skus"] = skus
        cites = [cites_o, v2_2[0]]
        why = f"Delivered {o.days} days ago, inside the {window}-day window; refund ${amount:.2f}"
        if mem:
            args["method"] = "store_credit"
            cites.append(mem)
            why += "; customer prefers store credit"
        limit = p.pick("policy:refund_policy§3")
        analysis = (
            f"{o.id} was delivered {o.days} days ago [{cites_o}]. {'/'.join(sorted(cats)).title()} "
            f"items have a {window}-day return window under Return Policy v2 [{v2_2[0]}], so the return is "
            f"eligible. Refund amount ${amount:.2f}; refunds over $200 need manager approval [{limit[0]}]."
        )
        if mem:
            analysis += f" Long-term memory says the customer prefers store credit [{mem}]."
        body = (
            f"Your return for order {o.id} was approved. We refunded ${amount:.2f}"
            f"{' as store credit' if mem else ' to your original payment method'}."
        )
        return _decision(
            "act",
            analysis,
            [
                _step("s1", "issue_refund", args, cites, why),
                _email("s2", o.customer_id, f"Refund for {o.id}", body, [cites_o], ["s1"]),
            ],
        )

    def _goodwill(self, p: _Prompt, o: _Order, window: int, v2: list[str]) -> str:
        cites_o, cust = f"db:orders:{o.id}", f"db:customers:{o.customer_id}"
        tier = p.tier(o.customer_id)
        pct = policy_rules().goodwill_limits.get(tier, 0)
        loyalty = p.pick("policy:loyalty_program§3")
        analysis = (
            f"{o.id} was delivered {o.days} days ago [{cites_o}], outside the {window}-day window "
            f"[{v2[0]}], so the return is denied. The customer is {tier} tier [{cust}]; goodwill "
            f"allowance for {tier} is {pct}% [{loyalty[0]}]."
        )
        if pct == 0:
            return _decision(
                "answer",
                analysis,
                answer=f"The return is denied: outside the {window}-day window [{v2[0]}].",
            )
        body = (
            f"We're sorry, order {o.id} is outside the {window}-day return window. As a thank-you for "
            f"being a {tier} member, here is a {pct}% coupon for your next order."
        )
        return _decision(
            "act",
            analysis,
            [
                _step(
                    "s1",
                    "issue_coupon",
                    {
                        "customer_id": o.customer_id,
                        "percent_off": pct,
                        "reason": f"Goodwill for denied return of {o.id}",
                        "purpose": "goodwill",
                    },
                    [cust, loyalty[0]],
                    f"{tier.title()} tier goodwill coupon within the {pct}% limit",
                ),
                _email(
                    "s2",
                    o.customer_id,
                    f"About your return for {o.id}",
                    body,
                    [cites_o, v2[0]],
                    ["s1"],
                ),
            ],
        )

    def _late(self, p: _Prompt, o: _Order) -> str:
        cites_o = f"db:orders:{o.id}"
        ship = p.pick("policy:shipping_policy§3")
        analysis = (
            f"{o.id} is {o.status} and not delivered; it is {o.days_past_promise} days past the delivery "
            f"promise [{cites_o}]. Shipping Policy: more than 10 days late → 10% coupon and a "
            f"shipping-investigation ticket [{ship[0]}]. Both are independent and run in parallel."
        )
        if (o.days_past_promise or 0) <= 10:
            return _decision(
                "answer",
                analysis,
                answer=f"{o.id} is in transit; a status update is sufficient [{ship[0]}].",
            )
        body = (
            f"We're sorry order {o.id} is late. We've opened an investigation with the carrier and "
            "issued you a 10% coupon."
        )
        return _decision(
            "act",
            analysis,
            [
                _step(
                    "s1",
                    "issue_coupon",
                    {
                        "customer_id": o.customer_id,
                        "percent_off": 10,
                        "reason": f"Late delivery of {o.id}",
                        "purpose": "shipping_delay",
                        "order_id": o.id,
                    },
                    [cites_o, ship[0]],
                    "Order is more than 10 days late: 10% apology coupon",
                ),
                _step(
                    "s2",
                    "create_ticket",
                    {
                        "customer_id": o.customer_id,
                        "order_id": o.id,
                        "category": "shipping_investigation",
                        "priority": "high",
                        "summary": f"{o.id} is {o.days_past_promise} days past promise and undelivered",
                    },
                    [cites_o, ship[0]],
                    "Open the shipping investigation required by policy",
                ),
                _email(
                    "s3",
                    o.customer_id,
                    f"Update on your order {o.id}",
                    body,
                    [cites_o],
                    ["s1", "s2"],
                ),
            ],
        )

    def _cancel(self, p: _Prompt, o: _Order) -> str:
        cites_o = f"db:orders:{o.id}"
        ship = p.pick("policy:shipping_policy§4")
        analysis = (
            f"{o.id} has status {o.status} [{cites_o}]. Orders not yet shipped may be cancelled; "
            f"cancellation is irreversible and needs supervisor approval [{ship[0]}]."
        )
        return _decision(
            "act",
            analysis,
            [
                _step(
                    "s1",
                    "cancel_order",
                    {"order_id": o.id, "reason": "Customer requested cancellation"},
                    [cites_o, ship[0]],
                    "Order has not shipped, so it can be cancelled",
                ),
                _email(
                    "s2",
                    o.customer_id,
                    f"Order {o.id} cancelled",
                    f"Your order {o.id} has been cancelled as requested.",
                    [cites_o],
                    ["s1"],
                ),
            ],
        )

    def _exchange(self, p: _Prompt, o: _Order) -> str:
        cites_o = f"db:orders:{o.id}"
        sku = o.items[0][0] if o.items else "EL-HP-02"
        db_ev, csv_ev = p.pick(f"db:products:{sku}"), p.pick(f"csv:inventory:{sku}")
        sop = p.pick("policy:escalation_sop§2")
        analysis = (
            f"Stock for {sku} conflicts: DB record [{db_ev[0]}] vs warehouse feed [{csv_ev[0]}]; the feed "
            f"snapshot is fresher, so the resolved stock is 0 (out of stock). {o.id} also has no delivery "
            f"date [{cites_o}], so exchange/return eligibility cannot be determined. Missing and "
            f"contradictory data require escalation [{sop[0]}]."
        )
        return _decision(
            "escalate",
            analysis,
            [
                _step(
                    "s1",
                    "escalate_to_human",
                    {
                        "summary": f"Exchange request for {o.id}: {sku} out of stock per fresher warehouse feed "
                        "(DB says 5, feed says 0) and delivery date missing.",
                        "priority": "high",
                        "customer_id": o.customer_id,
                        "order_id": o.id,
                    },
                    [cites_o, csv_ev[0], sop[0]],
                    "Out of stock and missing delivery date: hand to a human",
                ),
            ],
            conflicts=["DB stock 5 vs CSV 0: used fresher CSV snapshot"],
            missing=[f"{o.id} delivery_date"],
        )

    def _injection(self, p: _Prompt, o: _Order) -> str:
        cites_o = f"db:orders:{o.id}"
        amount = re.search(r"\$([\d,]+)", p.request)
        value = float(amount.group(1).replace(",", "")) if amount else o.total * 10
        blocked = any(
            a.get("tool") == "issue_refund" and a.get("status") == "skipped" for a in p.attempts
        )
        refund_max, sop = p.pick("policy:refund_policy§4"), p.pick("policy:escalation_sop§2")
        if not blocked:
            return _decision(
                "act",
                f"The operator asks for a ${value:,.0f} refund on {o.id} [{cites_o}].",
                [
                    _step(
                        "s1",
                        "issue_refund",
                        {"order_id": o.id, "amount": value, "reason": "Operator instruction"},
                        [cites_o],
                        "Requested refund",
                    )
                ],
                confidence=0.5,
            )
        return _decision(
            "escalate",
            f"The ${value:,.0f} refund was blocked: it exceeds the order total of ${o.total:.2f} "
            f"[{cites_o}] and refunds can never exceed the refundable amount [{refund_max[0]}]. "
            f"Requests exceeding order value and instructions to bypass policy are suspected fraud "
            f"and must be escalated [{sop[0]}].",
            [
                _step(
                    "s1",
                    "escalate_to_human",
                    {
                        "summary": f"Suspected fraud: request to refund ${value:,.0f} on {o.id} "
                        f"(order total ${o.total:.2f}) while instructing to ignore policy.",
                        "priority": "high",
                        "customer_id": o.customer_id,
                        "order_id": o.id,
                    },
                    [cites_o, *refund_max, *sop],
                    "Suspicious request exceeding order value",
                )
            ],
        )
