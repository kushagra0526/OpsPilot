"""Append-only audit trail of every proposed, approved, rejected, executed, skipped or refused action."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from opspilot import db

EVENTS = (
    "proposed",
    "approval_requested",
    "approved",
    "rejected",
    "executed",
    "failed",
    "skipped",
    "refused",
)


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, ensure_ascii=False)


def log_event(
    event: str,
    *,
    session_id: str,
    turn_id: str,
    tool: str | None = None,
    args: dict[str, Any] | None = None,
    rationale: str | None = None,
    risk_level: str | None = None,
    actor: str = "agent",
    result: dict[str, Any] | None = None,
) -> int:
    """Write one audit row and return its ID."""
    if event not in EVENTS:
        raise ValueError(f"unknown audit event {event!r}")
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO audit_log(ts, session_id, turn_id, event, tool, args_json, rationale,"
            " risk_level, actor, result_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="milliseconds"),
                session_id,
                turn_id,
                event,
                tool,
                _dump(args),
                rationale,
                risk_level,
                actor,
                _dump(result),
            ),
        )
        return int(cur.lastrowid or 0)


def get_audit(
    session_id: str | None = None, turn_id: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """Audit rows, newest first, optionally filtered by session and turn."""
    sql, params = "SELECT * FROM audit_log WHERE 1=1", []
    if session_id:
        sql += " AND session_id=?"
        params.append(session_id)
    if turn_id:
        sql += " AND turn_id=?"
        params.append(turn_id)
    sql += " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.query(sql, params)
