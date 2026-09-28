"""Parallel multi-source retrieval producing a RetrievalBundle of citable evidence."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from opspilot.agent.schemas import ConflictNote, Evidence, Intent, RetrievalBundle
from opspilot.config import MAX_POLICY_CHUNKS, POLICY_TOP_K, policy_rules, today
from opspilot.knowledge import faq, inventory, structured, vector_store

HINT_TOP_K = 2
CATEGORY_HINTS = {
    "electronics": "Return window for electronics and condition of opened electronics",
    "apparel": "Return window for apparel and home goods",
    "home": "Return window for apparel and home goods",
    "grocery": "Grocery and perishable items non-returnable",
}


def policy_label(meta: dict[str, Any]) -> str:
    """Readable citation label, e.g. 'Return Policy v2 §2' (version shown for versioned docs only)."""
    version = f" v{meta['version']}" if re.search(r"_v\d+$", meta["doc_id"]) else ""
    return f"{meta['title']}{version} §{meta['section']}"


def _policy_evidence(hit: dict[str, Any]) -> Evidence:
    meta = hit["metadata"]
    text = hit["document"].split(": ", 1)[-1]
    return Evidence(
        id=f"policy:{hit['id']}",
        source_type="policy",
        content=text,
        metadata={**meta, "label": policy_label(meta), "similarity": hit["similarity"]},
    )


def _hint_queries(orders: list[dict[str, Any]], intent: Intent) -> list[str]:
    """Deterministic extra policy queries derived from the referenced data."""
    hints: list[str] = []
    for order in orders:
        hints.extend(CATEGORY_HINTS[i["category"]] for i in order["items"])
        if order["status"] in ("placed", "shipped"):
            hints.append("Late delivery past the promise: coupon and shipping investigation")
            hints.append("Cancellation of orders not yet shipped")
        if order["status"] in ("delivered", "partially_refunded"):
            hints.append("Refund approval limits and maximum refundable amount")
            if _outside_window(order):
                hints.append("Goodwill coupon for out-of-window returns by loyalty tier")
        if not order["delivery_date"] and order["status"] == "delivered":
            hints.append("Escalate when data is missing and eligibility cannot be determined")
    if intent.request_type in ("action", "mixed"):
        hints.append("When to escalate to a human agent")
    return list(dict.fromkeys(hints))


def _outside_window(order: dict[str, Any]) -> bool:
    """True if any item is past its return window under the current policy's rules."""
    days = order["computed"]["days_since_delivery"]
    if days is None:
        return False
    windows = policy_rules().return_windows
    return any(
        windows.get(i["category"]) is not None and days > windows[i["category"]]
        for i in order["items"]
    )


def _doc_registry() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}
    for chunk in vector_store.get_policy_chunks():
        docs.setdefault(chunk["metadata"]["doc_id"], chunk["metadata"])
    return docs


def superseded_by(docs: dict[str, dict[str, Any]], ref: date) -> dict[str, str]:
    """Map old doc_id -> newer doc_id that supersedes it and is already effective."""
    result: dict[str, str] = {}
    for doc_id, meta in docs.items():
        old = meta.get("supersedes")
        if old and date.fromisoformat(meta["effective_date"]) <= ref:
            result[old] = doc_id
    return result


def _search_policies(
    user_input: str, intent: Intent, orders: list[dict[str, Any]]
) -> tuple[list[Evidence], list[ConflictNote], float]:
    hits = vector_store.search_policies([user_input, intent.summary], POLICY_TOP_K)
    max_sim = max((h["similarity"] for h in hits), default=0.0)
    for hint in _hint_queries(orders, intent):
        hits.extend(vector_store.search_policies([hint], HINT_TOP_K))
    seen: dict[str, dict[str, Any]] = {}
    for h in hits:
        if h["id"] not in seen or h["similarity"] > seen[h["id"]]["similarity"]:
            seen[h["id"]] = h
    ranked = sorted(seen.values(), key=lambda h: h["similarity"], reverse=True)[:MAX_POLICY_CHUNKS]
    evidence, conflicts = apply_versions(ranked, [user_input, intent.summary])
    return evidence, conflicts, max_sim


def apply_versions(
    hits: list[dict[str, Any]], texts: list[str]
) -> tuple[list[Evidence], list[ConflictNote]]:
    """Mark superseded chunks, pull in the superseding version and record conflict notes."""
    docs = _doc_registry()
    newer = superseded_by(docs, today())
    evidence = [_policy_evidence(h) for h in hits]
    conflicts: list[ConflictNote] = []
    present = {e.metadata["doc_id"] for e in evidence}
    for old_doc in sorted(
        {e.metadata["doc_id"] for e in evidence if e.metadata["doc_id"] in newer}
    ):
        new_doc = newer[old_doc]
        # The current counterpart of each outdated chunk: search the newer doc with the old
        # chunk's own text (v1 "30-day window" -> v2 "windows by category"), plus the request.
        old_texts = [h["document"] for h in hits if h["metadata"]["doc_id"] == old_doc]
        known = {e.id for e in evidence}
        for h in vector_store.search_policies(old_texts, 1, new_doc) + (
            [] if new_doc in present else vector_store.search_policies(texts, HINT_TOP_K, new_doc)
        ):
            if f"policy:{h['id']}" not in known:
                evidence.append(_policy_evidence(h))
                known.add(f"policy:{h['id']}")
        present.add(new_doc)
        for e in evidence:
            if e.metadata["doc_id"] == old_doc:
                e.superseded = True
                e.metadata["superseded_by"] = new_doc
        old_m, new_m = docs[old_doc], docs[new_doc]
        conflicts.append(
            ConflictNote(
                kind="policy_version",
                detail=(
                    f"{old_m['title']} v{old_m['version']} ({old_doc}, effective {old_m['effective_date']}) "
                    f"is superseded by v{new_m['version']} ({new_doc}, effective {new_m['effective_date']})."
                ),
                resolution=f"Applying {new_doc}; {old_doc} chunks are kept for reference only and must not be applied.",
                evidence_ids=[e.id for e in evidence if e.metadata["doc_id"] in (old_doc, new_doc)],
            )
        )
    return evidence, conflicts


def _order_task(order_id: str) -> tuple[dict[str, Any] | None, str]:
    return structured.get_order(order_id), order_id


def _customer_task(key: tuple[str, str]) -> tuple[dict[str, Any] | None, tuple[str, str]]:
    kind, value = key
    cust = (
        structured.get_customer(customer_id=value)
        if kind == "id"
        else structured.get_customer(email=value)
    )
    return cust, key


def _run_parallel(tasks: list[tuple[Callable[..., Any], Any]]) -> list[Any]:
    if not tasks:
        return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fn, arg) for fn, arg in tasks]
        return [f.result() for f in futures]


def memory_evidence(memories: list[dict[str, Any]]) -> list[Evidence]:
    """Long-term memories as citable evidence."""
    return [
        Evidence(
            id=f"memory:{m['id']}",
            source_type="memory",
            content=f"{m['subject_type'].title()} {m['subject_id']} {m['kind']}: {m['content']}",
            metadata={"created_at": m["created_at"], "source_session": m.get("source_session")},
        )
        for m in memories
    ]


def retrieve(
    intent: Intent, user_input: str, memories: list[dict[str, Any]] | None = None
) -> RetrievalBundle:
    """Query all knowledge sources (in parallel waves) and assemble the evidence bundle."""
    bundle = RetrievalBundle()
    # Wave 1: orders and explicitly referenced customers (policy search needs order data for hints).
    wave1 = [(_order_task, oid) for oid in intent.order_ids]
    wave1 += [(_customer_task, ("id", cid)) for cid in intent.customer_ids]
    wave1 += [(_customer_task, ("email", em)) for em in intent.emails]
    orders: list[dict[str, Any]] = []
    customers: dict[str, dict[str, Any]] = {}
    for result, key in _run_parallel(wave1):
        if isinstance(key, str):
            if result is None:
                bundle.missing.append(f"Order {key} not found")
            else:
                orders.append(result)
        elif result is None:
            label = "Customer" if key[0] == "id" else "Customer with email"
            bundle.missing.append(f"{label} {key[1]} not found")
        else:
            customers[result["id"]] = result

    # Wave 2: policies, FAQ, owners of referenced orders, and inventory for relevant SKUs.
    owner_ids = [o["customer_id"] for o in orders if o["customer_id"] not in customers]
    skus = list(dict.fromkeys(intent.skus + [i["sku"] for o in orders for i in o["items"]]))
    with ThreadPoolExecutor(max_workers=6) as pool:
        policy_f = pool.submit(_search_policies, user_input, intent, orders)
        faq_f = pool.submit(faq.search, [user_input, intent.summary])
        owner_f = [pool.submit(_customer_task, ("id", cid)) for cid in dict.fromkeys(owner_ids)]
        stock_f = {sku: pool.submit(inventory.get_stock, sku) for sku in skus}
        policy_ev, policy_conflicts, policy_sim = policy_f.result()
        faq_hits = faq_f.result()
        for fut in owner_f:
            cust, _ = fut.result()
            if cust:
                customers[cust["id"]] = cust
        stock_views = {sku: f.result() for sku, f in stock_f.items()}

    bundle.add(policy_ev)
    bundle.conflicts.extend(policy_conflicts)
    bundle.add([faq.to_evidence(h) for h in faq_hits])
    for order in orders:
        bundle.add([structured.order_evidence(order)])
        if order["status"] == "delivered" and not order["delivery_date"]:
            bundle.missing.append(
                f"{order['id']} has no delivery_date recorded (status delivered); return "
                "eligibility cannot be determined"
            )
    bundle.add([structured.customer_evidence(c) for c in customers.values()])
    for sku, view in stock_views.items():
        if view is None:
            bundle.missing.append(f"SKU {sku} not found")
            continue
        bundle.add(inventory.stock_evidence(view))
        if view["conflict"] is not None:
            bundle.conflicts.append(view["conflict"])
    bundle.add(memory_evidence(memories or []))
    bundle.max_similarity = max([policy_sim] + [h["similarity"] for h in faq_hits])
    bundle.entity_hits = (
        len(orders) + len(customers) + sum(v is not None for v in stock_views.values())
    )
    return bundle
