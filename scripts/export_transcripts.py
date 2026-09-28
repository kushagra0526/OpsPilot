"""Render the sample transcripts in docs/transcripts/ from recorded live runs (nothing is re-run).

Sources, all produced by the live Groq model:
- the live UI walkthrough's stored turns and audit rows (snapshotted from data/runtime into
  evals/evidence/transcript_sessions.json, so the transcripts survive a reseed);
- the two-operator stale-approval runs in evals/evidence/live_probes.jsonl.

Usage: python scripts/export_transcripts.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evals" / "evidence"
SNAPSHOT = EVIDENCE / "transcript_sessions.json"
OUT = ROOT / "docs" / "transcripts"
SESSIONS = {"approval": "sess-dda171", "conflict": "sess-a7ed36"}
MODEL = "openai/gpt-oss-120b (planning, responses) and openai/gpt-oss-20b (intent, memory) on Groq"


def snapshot() -> dict[str, Any]:
    """Copy the walkthrough sessions out of the runtime database once."""
    if SNAPSHOT.exists():
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{ROOT / 'data' / 'runtime' / 'opspilot.db'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    data: dict[str, Any] = {}
    for key, sid in SESSIONS.items():
        row = con.execute(
            "SELECT user_input, created_at, outcome_json FROM ui_turns WHERE session_id=?", (sid,)
        ).fetchone()
        audit = con.execute(
            "SELECT ts, event, tool, risk_level, actor, rationale FROM audit_log"
            " WHERE session_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        data[key] = {
            "session": sid,
            "input": row["user_input"],
            "at": row["created_at"],
            "outcome": json.loads(row["outcome_json"]),
            "audit": [dict(a) for a in audit],
        }
    SNAPSHOT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def _fence(obj: Any) -> str:
    return "```json\n" + json.dumps(obj, indent=2, ensure_ascii=False) + "\n```"


def _plan(out: dict[str, Any]) -> list[str]:
    lines = []
    for h in out["plan_history"]:
        d = h["decision"]
        lines += [
            f"### Iteration {h['iteration']}: decision `{d['decision_type']}`, confidence {d['confidence']}",
            "",
            "Analysis (model output, citations are evidence IDs):",
            "",
            "> " + d["analysis"].replace("\n", "\n> "),
            "",
        ]
        for s in d.get("steps") or []:
            after = ", ".join(s.get("depends_on") or []) or "none"
            lines += [
                f"- `{s['id']}` **{s['tool']}** (runs after: {after})",
                f"  - args: `{json.dumps(s['args'], ensure_ascii=False)}`",
                f"  - rationale: {s['rationale']}",
            ]
        lines.append("")
    return lines


def _risk(out: dict[str, Any]) -> list[str]:
    rows = ["| Step | Risk | Needs approval | Reasons |", "|---|---|---|---|"]
    for sid, r in (out.get("risk_assessments") or {}).items():
        rows.append(
            f"| {sid} | {r['level']} | {'yes' if r['requires_approval'] else 'no'} | "
            f"{'; '.join(r['reasons'])} |"
        )
    return rows


def _calls(out: dict[str, Any]) -> list[str]:
    lines = []
    t0 = min((r["started_at"] for r in out["step_results"] if r.get("started_at")), default=0)
    for r in out["step_results"]:
        timing = ""
        if r.get("started_at") and r.get("finished_at"):
            timing = (
                f" (start +{(r['started_at'] - t0) * 1000:.0f} ms, "
                f"took {(r['finished_at'] - r['started_at']) * 1000:.0f} ms)"
            )
        lines += [
            f"- `{r['step_id']}` **{r['tool']}**: `{r['status']}`{timing}",
            f"  - input: `{json.dumps(r['args'], ensure_ascii=False)}`",
            f"  - output: {(r.get('output') or {}).get('summary') or r.get('error') or 'none'}",
        ]
    return lines


def _audit(rows: list[dict[str, Any]]) -> list[str]:
    out = ["| Time | Event | Tool | Risk | Actor |", "|---|---|---|---|---|"]
    out += [
        f"| {a['ts'][11:23]} | {a['event']} | {a['tool'] or ''} | {a['risk_level'] or ''} | {a['actor']} |"
        for a in rows
    ]
    return out


def _result(goal: str, out: dict[str, Any]) -> dict[str, Any]:
    return {
        "goal": goal,
        "status": out["status"],
        "decision_type": (out.get("decision") or {}).get("decision_type"),
        "iterations": len(out["plan_history"]),
        "actions": [
            {
                "tool": r["tool"],
                "status": r["status"],
                "result": (r.get("output") or {}).get("summary") or r.get("error"),
            }
            for r in out["step_results"]
        ],
        "conflicts": [c["kind"] for c in out.get("conflicts") or []],
        "missing": out.get("missing") or [],
    }


def session_transcript(title: str, intro: str, rec: dict[str, Any], approval_note: str) -> str:
    """One recorded UI session as a transcript."""
    out = rec["outcome"]
    lines = [
        f"# {title}",
        "",
        intro,
        "",
        f"- Source: live UI walkthrough, session `{rec['session']}`, {rec['at']} "
        f"(stored turn and audit rows, snapshot in `evals/evidence/transcript_sessions.json`)",
        f"- Model: {MODEL}",
        "",
        "## Goal",
        "",
        f"> {rec['input']}",
        "",
        "## Planning trace (graph nodes in order)",
        "",
        *[f"1. `{t['node']}`: {t['summary']}" for t in out["trace"]],
        "",
        "## Plan",
        "",
        *_plan(out),
    ]
    if out.get("conflicts") or out.get("missing"):
        lines += ["## Conflicts and missing data found by retrieval", ""]
        lines += [f"- {c['kind']}: {c['detail']} {c['resolution']}" for c in out["conflicts"]]
        lines += [f"- missing: {m}" for m in out["missing"]]
        lines.append("")
    lines += ["## Risk and approval (code guardrails)", "", *_risk(out), "", approval_note, ""]
    lines += ["## Tool calls", "", *_calls(out), ""]
    lines += ["## Audit log", "", *_audit(rec["audit"]), ""]
    lines += ["## Final structured result", "", _fence(_result(rec["input"], out)), ""]
    lines += ["## Final explanation to the operator", "", out["final_response"], ""]
    return "\n".join(lines)


def probe_transcript() -> str:
    """The two-operator stale approval, before and after the replan fix."""
    probes = [
        json.loads(line)
        for line in (EVIDENCE / "live_probes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    runs = [p for p in probes if p.get("mode") == "stale-approval"]
    before, after = runs[0], runs[-1]

    def section(label: str, p: dict[str, Any]) -> list[str]:
        other = p["other_session"]
        return [
            f"## {label} ({p['at']})",
            "",
            f"Operator B's session `{other['session']}`: "
            + ", ".join(f"{r['tool']} {r['status']}" for r in other["step_results"])
            + ".",
            "",
            f"Operator A's session `{p['session']}`, after A approves:",
            "",
            "Planning trace:",
            "",
            *[f"1. `{t}`" for t in p["trace"]],
            "",
            "Plans per iteration:",
            "",
            *[
                f"- iteration {pl['iteration']}: `{pl['decision_type']}`, steps "
                + (
                    ", ".join(
                        f"{s['tool']} (after {', '.join(s['depends_on']) or 'none'})"
                        for s in pl["steps"]
                    )
                    or "none"
                )
                for pl in p["plans"]
            ],
            "",
            "Tool results:",
            "",
            *[
                f"- iteration {r['iteration']} **{r['tool']}**: `{r['status']}`"
                + (f", reason: {r['reason']}" if r.get("reason") else "")
                for r in p["step_results"]
            ],
            "",
            "Audit log:",
            "",
            "| Event | Tool | Actor |",
            "|---|---|---|",
            *[f"| {a['event']} | {a['tool'] or ''} | {a['actor']} |" for a in p["audit"]],
            "",
            "Final structured result:",
            "",
            _fence(
                {
                    "goal": p["prompt"],
                    "status": p["status"],
                    "iterations": p["iterations"],
                    "final_decision": p["plans"][-1]["decision_type"],
                    "actions": [
                        {"tool": r["tool"], "status": r["status"]} for r in p["step_results"]
                    ],
                }
            ),
            "",
            "Final explanation to the operator:",
            "",
            p["final_response"],
            "",
        ]

    head = [
        "# 03: Failure and self-correction: a stale approval",
        "",
        "Two operators open the same request in two sessions. Both plans pause for approval. Operator B "
        "approves first and the refund runs. Then operator A approves the plan that is now stale. The "
        "executor re-checks every precondition right before running a step, so A's refund is blocked; "
        "the observe node sees the blocked step and sends the agent back to planning.",
        "",
        "The same live scenario was recorded twice: before and after a fix to the replan step. In the first "
        "run the guardrail held (no second refund), but the replan still used the order record read at the "
        "start of the turn, so the model proposed the same refund again until the 3-iteration cap and its "
        "explanation repeated the stale amount. The fix re-reads the records behind a blocked or failed step "
        "before replanning (`_refresh_records` in `src/opspilot/agent/nodes.py`, test "
        "`test_stale_approval_replans_on_current_records`). The second run shows the corrected behaviour.",
        "",
        "- Source: `evals/evidence/live_probes.jsonl` (both runs, recorded as they happened)",
        f"- Model: {MODEL}",
        "",
        "## Goal (both operators)",
        "",
        f"> {after['prompt']}",
        "",
    ]
    return "\n".join(
        head
        + section("Run 1: before the fix", before)
        + section("Run 2: after the fix (records re-read before replanning)", after)
    )


def main() -> None:
    """Write the three transcripts."""
    data = snapshot()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "01_planning_approval.md").write_text(
        session_transcript(
            "01: Multi-step planning with human approval",
            "A $1,299 refund request. The model checks eligibility against the current policy, plans the "
            "refund and a dependent confirmation email, the risk guardrail pauses the refund for a person, "
            "and after approval both steps run in dependency order.",
            data["approval"],
            "The refund paused at the approval gate. Operator `operator-1` approved it in the UI with the "
            'note "Photo of the dead pixel checked"; the email (low risk) waited for the refund.',
        ),
        encoding="utf-8",
    )
    (OUT / "02_conflicting_incomplete_data.md").write_text(
        session_transcript(
            "02: Conflicting and incomplete data",
            "An exchange request for an order with no recorded delivery date, for a product whose stock "
            "differs between the database (5) and the warehouse feed (0). Retrieval surfaces both "
            "problems; the model resolves the stock conflict by the fresher source and escalates the "
            "exchange because eligibility cannot be determined.",
            data["conflict"],
            "No step needed approval: escalation and email are low risk.",
        ),
        encoding="utf-8",
    )
    (OUT / "03_failure_and_self_correction.md").write_text(probe_transcript(), encoding="utf-8")
    print("wrote", ", ".join(p.name for p in sorted(OUT.glob("*.md"))))


if __name__ == "__main__":
    main()
