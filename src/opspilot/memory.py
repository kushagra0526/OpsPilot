"""Long-term memory: load at turn start, extract durable facts at turn end, apply preferences."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from opspilot import db
from opspilot.agent import prompts
from opspilot.config import MEMORY_LOAD_LIMIT
from opspilot.llm import DailyLimitError, LLMClient, complete_structured


class ExtractedMemory(BaseModel):
    """One durable fact proposed by the extraction model."""

    subject_type: Literal["customer", "operator"]
    subject_id: str
    kind: Literal["preference", "fact"]
    content: str
    refund_method: Literal["store_credit", "original"] | None = None


class MemoryExtraction(BaseModel):
    """Extraction model output."""

    memories: list[ExtractedMemory] = []
    interaction_summary: str = ""


def load_memories(
    customer_ids: list[str], operator_id: str | None = None, limit: int = MEMORY_LOAD_LIMIT
) -> list[dict[str, Any]]:
    """Memories for the customers + operator, newest first; preferences/facts before summaries."""
    subjects = [("customer", c) for c in dict.fromkeys(customer_ids)]
    if operator_id:
        subjects.append(("operator", operator_id))
    if not subjects:
        return []
    where = " OR ".join("(subject_type=? AND subject_id=?)" for _ in subjects)
    params = [v for pair in subjects for v in pair]
    rows = db.query(
        f"SELECT * FROM long_term_memory WHERE {where} "
        "ORDER BY (kind='interaction_summary'), id DESC",
        params,
    )
    return rows[:limit]


def save_memory(
    subject_type: str, subject_id: str, kind: str, content: str, session_id: str
) -> int | None:
    """Insert one memory; returns its ID, or None if it already exists (UNIQUE dedupe)."""
    try:
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO long_term_memory(subject_type, subject_id, kind, content,"
                " source_session, created_at) VALUES (?,?,?,?,?,?)",
                (
                    subject_type,
                    subject_id,
                    kind,
                    content.strip(),
                    session_id,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            return int(cur.lastrowid or 0)
    except sqlite3.IntegrityError:
        return None


def apply_preference(customer_id: str, refund_method: str) -> None:
    """Persist a learned refund-method preference on the customer record."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE customers SET preferred_refund_method=? WHERE id=?",
            (refund_method, customer_id),
        )


def _known_customers(ids: list[str]) -> set[str]:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {r["id"] for r in db.query(f"SELECT id FROM customers WHERE id IN ({marks})", ids)}


def extract_and_save(
    llm: LLMClient,
    *,
    session_id: str,
    user_input: str,
    final_response: str,
    customer_ids: list[str],
    operator_id: str,
    fallback_summary: str,
) -> list[dict[str, Any]]:
    """Run the small model, keep only valid subjects, save and apply preferences."""
    try:
        extraction = complete_structured(
            llm,
            "memory",
            prompts.MEMORY_SYSTEM,
            prompts.build_memory_user(
                user_input=user_input,
                final_response=final_response,
                customer_ids=customer_ids,
                operator_id=operator_id,
            ),
            MemoryExtraction,
            tier="small",
        )
    except DailyLimitError:
        raise
    except Exception:
        extraction = MemoryExtraction()
    mentioned = [m.subject_id for m in extraction.memories if m.subject_type == "customer"]
    valid = _known_customers(list(dict.fromkeys(customer_ids + mentioned)))
    saved: list[dict[str, Any]] = []
    for mem in extraction.memories:
        if mem.subject_type == "customer" and mem.subject_id not in valid:
            continue
        if mem.subject_type == "operator" and mem.subject_id != operator_id:
            continue
        mid = save_memory(mem.subject_type, mem.subject_id, mem.kind, mem.content, session_id)
        if mem.subject_type == "customer" and mem.refund_method:
            apply_preference(mem.subject_id, mem.refund_method)
        saved.append({**mem.model_dump(), "id": mid, "new": mid is not None})
    summary = extraction.interaction_summary.strip() or fallback_summary
    for cid in customer_ids:
        if cid in valid and summary:
            mid = save_memory("customer", cid, "interaction_summary", summary, session_id)
            saved.append(
                {
                    "subject_type": "customer",
                    "subject_id": cid,
                    "kind": "interaction_summary",
                    "content": summary,
                    "id": mid,
                    "new": mid is not None,
                }
            )
    return saved


def list_memories(subject_id: str | None = None) -> list[dict[str, Any]]:
    """All memories (optionally for one subject), newest first."""
    if subject_id:
        return db.query(
            "SELECT * FROM long_term_memory WHERE subject_id=? ORDER BY id DESC", (subject_id,)
        )
    return db.query("SELECT * FROM long_term_memory ORDER BY id DESC")


def delete_memory(memory_id: int) -> None:
    """Delete one memory row."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM long_term_memory WHERE id=?", (memory_id,))
