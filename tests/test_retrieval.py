"""Retrieval: versioned policies, computed order fields, missing data and relevance."""

from __future__ import annotations

from opspilot.agent.schemas import Intent
from opspilot.config import RELEVANCE_THRESHOLD
from opspilot.knowledge.retriever import retrieve


def _intent(text: str, **kw) -> Intent:
    return Intent(summary=text, request_type=kw.pop("request_type", "question"), **kw)


def test_return_window_uses_v2_and_marks_v1_superseded() -> None:
    text = "What is the return window for headphones?"
    bundle = retrieve(_intent(text), text)
    policy = [e for e in bundle.evidence if e.source_type == "policy"]
    v2 = [e for e in policy if e.metadata["doc_id"] == "return_policy_v2"]
    v1 = [e for e in policy if e.metadata["doc_id"] == "return_policy_v1"]
    assert v2 and all(not e.superseded for e in v2)
    assert v1 and all(e.superseded for e in v1)
    assert any(e.id == "policy:return_policy_v2§2" for e in v2)
    notes = [c for c in bundle.conflicts if c.kind == "policy_version"]
    assert notes and "return_policy_v2" in notes[0].resolution


def test_order_evidence_has_computed_fields() -> None:
    text = "Customer C-001 wants to return ORD-1007"
    bundle = retrieve(_intent(text, order_ids=["ORD-1007"], customer_ids=["C-001"]), text)
    order = bundle.get("db:orders:ORD-1007")
    assert order is not None
    computed = order.metadata["computed"]
    assert computed["days_since_delivery"] == 9
    assert computed["refundable_remaining"] == 149.0
    assert bundle.get("db:customers:C-001") is not None
    assert "9 days ago" in order.content


def test_late_order_days_past_promise() -> None:
    text = "Where is ORD-1003?"
    bundle = retrieve(_intent(text, order_ids=["ORD-1003"]), text)
    order = bundle.get("db:orders:ORD-1003")
    assert order and order.metadata["computed"]["days_past_promise"] == 15
    assert bundle.get("db:customers:C-012") is not None, "order owner is pulled in"
    assert any(e.id == "policy:shipping_policy§3" for e in bundle.evidence)


def test_missing_delivery_date_is_reported() -> None:
    text = "Refund ORD-1004 please."
    bundle = retrieve(_intent(text, order_ids=["ORD-1004"]), text)
    assert any("ORD-1004 has no delivery_date" in m for m in bundle.missing)


def test_unknown_order_not_found() -> None:
    text = "Refund ORD-9999"
    bundle = retrieve(_intent(text, order_ids=["ORD-9999"]), text)
    assert "Order ORD-9999 not found" in bundle.missing
    assert bundle.get("db:orders:ORD-9999") is None


def test_unknown_customer_not_found() -> None:
    text = "Look up C-999"
    bundle = retrieve(_intent(text, customer_ids=["C-999"]), text)
    assert "Customer C-999 not found" in bundle.missing


def test_memories_become_evidence() -> None:
    mem = {
        "id": 5,
        "subject_type": "customer",
        "subject_id": "C-007",
        "kind": "preference",
        "content": "Prefers store credit",
        "created_at": "2026-09-27",
        "source_session": "a",
    }
    bundle = retrieve(_intent("refund ORD-1006", order_ids=["ORD-1006"]), "refund ORD-1006", [mem])
    ev = bundle.get("memory:5")
    assert ev and ev.source_type == "memory" and "store credit" in ev.content


def test_faq_is_searched() -> None:
    text = "How do I use a coupon code?"
    bundle = retrieve(_intent(text), text)
    assert any(e.source_type == "faq" for e in bundle.evidence)
    assert bundle.max_similarity >= RELEVANCE_THRESHOLD


def test_out_of_scope_is_below_relevance_threshold() -> None:
    text = "What's the weather in Mumbai today?"
    bundle = retrieve(_intent(text, request_type="out_of_scope"), text)
    assert bundle.entity_hits == 0
    assert bundle.max_similarity < RELEVANCE_THRESHOLD


def test_first_client_creation_is_thread_safe(tmp_path, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from opspilot.knowledge import vector_store

    # Own runtime: racing new clients must not touch the shared session store.
    monkeypatch.setenv("OPSPILOT_RUNTIME_DIR", str(tmp_path))
    vector_store.ingest_all()
    vector_store._clients.pop(str(tmp_path / "chroma"), None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda q: vector_store.search_faq([q], 1), ["coupon", "refund"] * 2)
        )
    assert all(results)


def test_superseded_chunk_brings_its_current_counterpart() -> None:
    text = "C-001 wants to return the Trail Jacket from ORD-1002."
    intent = _intent(text, order_ids=["ORD-1002"], customer_ids=["C-001"], request_type="action")
    bundle = retrieve(intent, text)
    ids = {e.id for e in bundle.evidence}
    assert "policy:return_policy_v2§2" in ids, "current category windows must accompany v1's window"


def test_rebuild_waits_until_queryable_and_fails_loudly() -> None:
    import pytest

    from opspilot.knowledge import vector_store

    vector_store._wait_until_queryable("policies", 38, timeout=5)  # ready store: returns
    with pytest.raises(RuntimeError, match="not queryable"):
        vector_store._wait_until_queryable("policies", 999, timeout=0.5)


def test_out_of_window_order_retrieves_goodwill_rule() -> None:
    text = "Customer C-004 wants to return everything from ORD-3002."
    intent = _intent(text, order_ids=["ORD-3002"], customer_ids=["C-004"], request_type="action")
    ids = {e.id for e in retrieve(intent, text).evidence}
    assert "policy:loyalty_program§3" in ids, "an item past its window makes goodwill relevant"
