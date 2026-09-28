"""Long-term memory across sessions (SPEC §9, Phase 5)."""

from __future__ import annotations

import json
import re

from fakes import FakeLLM, new_session, plan, service, step

from opspilot import db, memory
from opspilot.service import AgentService

PREF = json.dumps(
    {
        "memories": [
            {
                "subject_type": "customer",
                "subject_id": "C-007",
                "kind": "preference",
                "content": "Prefers store credit over card refunds",
                "refund_method": "store_credit",
            }
        ],
        "interaction_summary": "Operator noted Priya's store-credit preference.",
    }
)


def _ack(system: str, user: str) -> str:
    return plan(
        "answer",
        "Noted for C-007 [db:customers:C-007].",
        answer="Noted: C-007 prefers store credit [db:customers:C-007].",
    )


def _refund_citing_memory(system: str, user: str) -> str:
    mem_id = re.search(r"\[(memory:\d+)\][^\n]*store credit", user).group(1)
    order = "db:orders:ORD-1006"
    return plan(
        "act",
        f"Jacket is apparel within 30 days [{order}]; customer prefers store credit [{mem_id}].",
        [
            step(
                "s1",
                "issue_refund",
                {
                    "order_id": "ORD-1006",
                    "amount": 89,
                    "reason": "jacket return",
                    "method": "store_credit",
                    "skus": ["AP-JKT-03"],
                },
                [order, mem_id],
            ),
        ],
    )


def test_preference_learned_in_a_and_applied_in_b(fresh_db) -> None:
    svc_a = AgentService(llm=FakeLLM({"plan": [_ack], "memory": [PREF]}))
    svc_a.start_turn(
        new_session(), "Customer C-007 says she always prefers store credit instead of card refunds"
    )
    prefs = [m for m in memory.list_memories("C-007") if m["kind"] == "preference"]
    assert [m["content"] for m in prefs] == ["Prefers store credit over card refunds"]
    assert db.query_one("SELECT preferred_refund_method FROM customers WHERE id='C-007'") == {
        "preferred_refund_method": "store_credit"
    }
    assert any(m["kind"] == "interaction_summary" for m in memory.list_memories("C-007"))

    llm_b = FakeLLM({"plan": [_refund_citing_memory]})
    svc_b = AgentService(llm=llm_b)
    out = svc_b.start_turn(new_session(), "Refund the jacket from ORD-1006.")
    plan_prompt = next(c["user"] for c in llm_b.calls if c["node"] == "plan")
    memory_id = f"memory:{prefs[0]['id']}"
    assert memory_id in plan_prompt and "RELEVANT LONG-TERM MEMORIES" in plan_prompt
    assert "Prefers store credit over card refunds" in plan_prompt
    [refund] = db.query("SELECT * FROM refunds WHERE order_id='ORD-1006'")
    assert refund["method"] == "store_credit" and refund["amount"] == 89
    assert memory_id in out.decision["analysis"]
    assert any(e["id"] == memory_id for e in out.evidence_used)


def test_code_applies_preference_when_plan_omits_method(fresh_db) -> None:
    memory.apply_preference("C-007", "store_credit")
    order = "db:orders:ORD-1006"
    svc, _ = service(
        [
            plan(
                "act",
                f"Eligible [{order}].",
                [
                    step(
                        "s1",
                        "issue_refund",
                        {
                            "order_id": "ORD-1006",
                            "amount": 89,
                            "reason": "x",
                            "skus": ["AP-JKT-03"],
                        },
                        [order],
                    )
                ],
            )
        ]
    )
    svc.start_turn(new_session(), "Refund the jacket from ORD-1006.")
    assert (
        db.query_one("SELECT method FROM refunds WHERE order_id='ORD-1006'")["method"]
        == "store_credit"
    )


def test_dedupe_and_subject_validation(fresh_db) -> None:
    bogus = json.dumps(
        {
            "memories": [
                {
                    "subject_type": "customer",
                    "subject_id": "C-007",
                    "kind": "preference",
                    "content": "Likes jackets",
                },
                {
                    "subject_type": "customer",
                    "subject_id": "C-999",
                    "kind": "fact",
                    "content": "Invented",
                },
            ],
            "interaction_summary": "",
        }
    )
    llm = FakeLLM({"memory": [bogus, bogus]})
    kwargs = {
        "session_id": "s",
        "user_input": "u",
        "final_response": "r",
        "customer_ids": ["C-007"],
        "operator_id": "operator-1",
        "fallback_summary": "summary",
    }
    first = memory.extract_and_save(llm, **kwargs)
    second = memory.extract_and_save(llm, **kwargs)
    assert [m["subject_id"] for m in first if m["kind"] == "preference"] == ["C-007"]
    assert not memory.list_memories("C-999")
    assert all(not m["new"] for m in second), "UNIQUE constraint dedupes"


def test_load_limit_and_priority(fresh_db) -> None:
    for i in range(12):
        memory.save_memory("customer", "C-001", "interaction_summary", f"turn {i}", "s")
    memory.save_memory("customer", "C-001", "preference", "Prefers email contact", "s")
    loaded = memory.load_memories(["C-001"])
    assert len(loaded) == 10
    assert loaded[0]["kind"] == "preference", "durable facts outrank interaction summaries"
    assert loaded[1]["content"] == "turn 11", "newest first"


def test_operator_memories_and_delete(fresh_db) -> None:
    mid = memory.save_memory("operator", "operator-1", "preference", "Always cc the team lead", "s")
    assert any(m["id"] == mid for m in memory.load_memories([], "operator-1"))
    memory.delete_memory(mid)
    assert not memory.load_memories([], "operator-1")
