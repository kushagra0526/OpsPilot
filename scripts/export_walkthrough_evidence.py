"""Export the evidence of a live UI walkthrough from the app's own runtime database.

The Playwright walkthrough (site/scripts/live-walkthrough.mjs) drives the Streamlit app and records
which session each scenario ran in (evals/logs/walkthrough.json). This script reads, for those
sessions, the stored turn outcomes (plan, steps, trace, final response), every audit row, the
long-term memories and the business rows the actions changed, then checks the safety invariants:
a rejected step never executes, and every executed step was proposed (and approved when approval
was requested) earlier in the audit log.

Writes evals/evidence/walkthrough.json and evals/evidence/walkthrough.md.
Usage: python scripts/export_walkthrough_evidence.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from opspilot import db

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "evals" / "logs" / "walkthrough.json"
OUT = ROOT / "evals" / "evidence"


def _turns(session: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT user_input, status, outcome_json FROM ui_turns WHERE session_id=? ORDER BY rowid",
        (session,),
    )
    return [{"input": r["user_input"], **json.loads(r["outcome_json"])} for r in rows]


def _audit(session: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT id, ts, event, tool, risk_level, actor, rationale FROM audit_log"
        " WHERE session_id=? ORDER BY id",
        (session,),
    )


def overlaps(results: list[dict[str, Any]]) -> list[str]:
    """Pairs of executed steps whose run times overlapped (evidence of parallel execution)."""
    timed = [
        r
        for r in results
        if r["status"] == "executed" and r.get("started_at") and r.get("finished_at")
    ]
    pairs = []
    for i, a in enumerate(timed):
        for b in timed[i + 1 :]:
            if a["started_at"] < b["finished_at"] and b["started_at"] < a["finished_at"]:
                pairs.append(f"{a['tool']} || {b['tool']}")
    return pairs


def invariants(audit_rows: list[dict[str, Any]]) -> list[str]:
    """Safety violations in one session's audit trail (empty when all hold)."""
    problems: list[str] = []
    seen: dict[str, set[str]] = {}
    for row in audit_rows:
        tool = row["tool"] or ""
        events = seen.setdefault(tool, set())
        if row["event"] == "executed":
            if "rejected" in events:
                problems.append(f"{tool} executed after it was rejected")
            if "proposed" not in events:
                problems.append(f"{tool} executed without a proposal row")
            if "approval_requested" in events and "approved" not in events:
                problems.append(f"{tool} executed without an approval row")
        events.add(row["event"])
    return problems


def scenario_record(step: dict[str, Any]) -> dict[str, Any]:
    """Everything stored for one walkthrough step's session."""
    session = step["session"]
    turns, rows = _turns(session), _audit(session)
    last = turns[-1] if turns else {}
    return {
        "label": step["label"],
        "session": session,
        "turns": len(turns),
        "status": last.get("status"),
        "decision_type": (last.get("decision") or {}).get("decision_type"),
        "iterations": len(last.get("plan_history") or []),
        "steps": [
            {"step": r["step_id"], "tool": r["tool"], "status": r["status"]}
            for t in turns
            for r in t.get("step_results") or []
        ],
        "parallel": [p for t in turns for p in overlaps(t.get("step_results") or [])],
        "conflicts": [c["kind"] for c in last.get("conflicts") or []],
        "trace": [t["node"] for t in last.get("trace") or []],
        "tokens": sum(
            (t.get("usage") or {}).get("turn", {}).get("input_tokens", 0)
            + (t.get("usage") or {}).get("turn", {}).get("output_tokens", 0)
            for t in turns
        ),
        "final_response": last.get("final_response") or "",
        "audit": rows,
        "violations": invariants(rows),
    }


def business_rows() -> dict[str, list[dict[str, Any]]]:
    """The rows the walkthrough's actions wrote (what the app's data tab shows)."""
    return {
        "orders": db.query(
            "SELECT id, status FROM orders WHERE id BETWEEN 'ORD-1001' AND 'ORD-1008' ORDER BY id"
        ),
        "refunds": db.query(
            "SELECT order_id, amount, method, approved_by FROM refunds ORDER BY id"
        ),
        "coupons": db.query("SELECT customer_id, percent_off, reason FROM coupons ORDER BY rowid"),
        "tickets": db.query("SELECT order_id, category, priority FROM tickets ORDER BY rowid"),
        "emails": db.query("SELECT to_email, subject FROM outbox ORDER BY id"),
    }


def markdown(payload: dict[str, Any]) -> str:
    """Readable summary of the walkthrough evidence."""
    meta, records, checks = payload["meta"], payload["scenarios"], payload["ui_checks"]
    lines = [
        "# Live UI walkthrough evidence",
        "",
        f"- App: {meta['app']} · finished {meta['finished']}",
        f"- Model: {meta['model']}",
        f"- Browser console errors: {meta['console_errors']}",
        "",
        "| Step | Session | Decision | Steps (tool: status) | Ran in parallel | Audit events | Violations |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in records:
        steps = ", ".join(f"{s['tool']}: {s['status']}" for s in r["steps"]) or "none"
        events = ", ".join(
            f"{a['event']}" + (f" {a['tool']}" if a["tool"] else "") for a in r["audit"]
        )
        lines.append(
            f"| {r['label']} | `{r['session']}` | {r['decision_type']} | {steps}"
            f" | {', '.join(r['parallel']) or 'none'} | {events or 'none'}"
            f" | {'; '.join(r['violations']) or 'none'} |"
        )
    lines += ["", "## UI checks", ""]
    lines += [
        f"- {c['label']}: {json.dumps({k: v for k, v in c.items() if k != 'label'})}"
        for c in checks
    ]
    lines += ["", "## Long-term memory after the walkthrough", ""]
    lines += [
        f"- {m['subject_type']} {m['subject_id']} ({m['kind']}): {m['content']}"
        for m in payload["memories"]
    ]
    lines += ["", "## Business rows after the walkthrough (what the data tab shows)", ""]
    for name, rows in payload["business_rows_after"].items():
        cells = "; ".join(json.dumps(r, ensure_ascii=False) for r in rows)
        lines.append(f"- **{name}**: {cells or 'none'}")
    return "\n".join(lines) + "\n"


def main() -> None:
    """Build the evidence files from the walkthrough log and the runtime database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=LOG, help="walkthrough summary to read")
    parser.add_argument("--out", type=Path, default=OUT, help="directory for the evidence files")
    args = parser.parse_args()
    log = json.loads(args.log.read_text(encoding="utf-8"))
    scenario_steps = [s for s in log["steps"] if s.get("session")]
    ui_checks = [s for s in log["steps"] if not s.get("session")]
    records = [scenario_record(s) for s in scenario_steps]
    meta = {
        "app": log["app"],
        "finished": log["finished"],
        "model": log.get("model") or "not recorded",
        "console_errors": len(log.get("consoleErrors") or []),
    }
    memories = db.query(
        "SELECT subject_type, subject_id, kind, content, source_session FROM long_term_memory"
    )
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "scenarios": records,
        "ui_checks": ui_checks,
        "memories": memories,
        "business_rows_after": business_rows(),
    }
    (args.out / "walkthrough.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (args.out / "walkthrough.md").write_text(markdown(payload), encoding="utf-8")
    bad = [r for r in records if r["violations"]]
    print(f"{len(records)} sessions exported; {len(bad)} with safety violations")


if __name__ == "__main__":
    main()
