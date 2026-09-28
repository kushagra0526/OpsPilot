"""OpsPilot CLI: interactive chat with plan/approval panels, plus audit and memory views."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

app = typer.Typer(help="OpsPilot: autonomous knowledge execution agent for NovaMart support ops.")
console = Console()
RISK_STYLE = {"low": "green", "medium": "yellow", "high": "bold red"}
STATUS_STYLE = {"executed": "green", "failed": "red", "skipped": "dim", "rejected": "red"}


def _service() -> Any:
    from opspilot.llm import MissingAPIKeyError
    from opspilot.service import AgentService

    try:
        return AgentService()
    except MissingAPIKeyError:
        console.print(
            Panel(
                "GROQ_API_KEY is not set. Get a free key at https://console.groq.com, "
                "and put it in .env as GROQ_API_KEY.",
                title="Setup needed",
                style="yellow",
            )
        )
        raise typer.Exit(1) from None


def _show_plan(out: Any) -> None:
    for h in out.plan_history:
        d = h["decision"]
        console.print(
            Panel(
                Markdown(d["analysis"]),
                title=f"Reasoning · iteration {h['iteration']} · {d['decision_type']} · "
                f"confidence {d['confidence']:.2f}",
                border_style="cyan",
            )
        )
    for c in out.conflicts:
        console.print(f"[yellow]{c['kind']} conflict:[/] {c['detail']} [dim]→ {c['resolution']}[/]")
    for m in out.missing:
        console.print(f"[yellow]? missing:[/] {m}")
    steps = (out.decision or {}).get("steps") or []
    if steps:
        table = Table(title="Plan", show_lines=True)
        for col in ("step", "tool", "args", "risk", "depends_on", "rationale"):
            table.add_column(col)
        for s in steps:
            r = out.risk_assessments.get(s["id"], {})
            level = r.get("level", "?")
            risk = f"[{RISK_STYLE.get(level, '')}]{level}{' (approval)' if r.get('requires_approval') else ''}[/]"
            table.add_row(
                s["id"],
                s["tool"],
                json.dumps(s["args"], ensure_ascii=False),
                risk,
                ", ".join(s["depends_on"]) or "-",
                s["rationale"],
            )
        console.print(table)


def _show_results(out: Any) -> None:
    if not out.step_results:
        return
    table = Table(title="Execution")
    for col in ("iter", "step", "tool", "status", "result / reason", "ms"):
        table.add_column(col)
    for r in out.step_results:
        ms = f"{(r['finished_at'] - r['started_at']) * 1000:.0f}" if r.get("finished_at") else "-"
        summary = (r.get("output") or {}).get("summary") or r.get("error") or ""
        style = STATUS_STYLE.get(r["status"], "")
        table.add_row(
            str(r["iteration"]), r["step_id"], r["tool"], f"[{style}]{r['status']}[/]", summary, ms
        )
    console.print(table)


def _ask_approvals(pending: list[dict[str, Any]], operator: str) -> dict[str, dict[str, Any]]:
    decisions = {}
    for p in pending:
        console.print(
            Panel(
                "\n".join(f"• {x}" for x in p["reasons"]) + f"\n\n{p['rationale']}",
                title=f"Approval needed: {p['step_id']} {p['tool']} {json.dumps(p['args'], ensure_ascii=False)}",
                border_style="red",
            )
        )
        ok = Confirm.ask(f"Approve {p['step_id']}?", default=False)
        note = Prompt.ask("Note", default="")
        decisions[p["step_id"]] = {"approved": ok, "approver": operator, "note": note}
    return decisions


@app.command()
def chat(
    session: str = typer.Option("session1", help="Session id (thread) to use"),
    operator: str = typer.Option("operator-1", help="Operator name used for approvals"),
) -> None:
    """Interactive chat. Type 'exit' to quit."""
    from opspilot.llm import LLMError
    from opspilot.service import PendingApprovalError

    svc = _service()
    info = svc.llm.describe()
    console.print(
        Panel(
            f"Session [bold]{session}[/] · operator {operator} · model {info['provider']} "
            f"({info['large']})",
            title="OpsPilot",
        )
    )
    while True:
        pending = svc.pending_approvals(session)
        if pending:
            console.print("[yellow]This session has steps awaiting approval.[/]")
            out = svc.resume_with_approval(session, _ask_approvals(pending, operator))
        else:
            text = Prompt.ask("[bold cyan]operator[/]")
            if text.strip().lower() in {"exit", "quit", "/q"}:
                break
            if not text.strip():
                continue
            try:
                with console.status("Retrieving, reasoning, planning…"):
                    out = svc.start_turn(session, text, operator)
            except (LLMError, PendingApprovalError) as exc:
                console.print(f"[red]{exc}[/]")
                continue
        _show_plan(out)
        if out.status == "awaiting_approval":
            decisions = _ask_approvals(out.pending_approvals, operator)
            with console.status("Executing…"):
                out = svc.resume_with_approval(session, decisions)
        _show_results(out)
        console.print(
            Panel(Markdown(out.final_response or ""), title="OpsPilot", border_style="green")
        )
        usage = out.usage.get("turn", {})
        console.print(
            f"[dim]LLM calls {usage.get('requests', 0)}, tokens "
            f"{usage.get('input_tokens', 0) + usage.get('output_tokens', 0)}[/]"
        )


@app.command()
def audit(
    session: str | None = typer.Option(None, help="Filter by session"),
    limit: int = typer.Option(40, help="Rows to show"),
) -> None:
    """Show the audit log (newest first)."""
    from opspilot.audit import get_audit

    table = Table(title="Audit log")
    for col in ("id", "ts", "session", "event", "tool", "risk", "actor", "rationale"):
        table.add_column(col, overflow="fold")
    for r in get_audit(session, limit=limit):
        table.add_row(
            str(r["id"]),
            r["ts"][:19],
            r["session_id"] or "",
            r["event"],
            r["tool"] or "",
            r["risk_level"] or "",
            r["actor"] or "",
            (r["rationale"] or "")[:120],
        )
    console.print(table)


@app.command()
def memory(
    subject: str | None = typer.Option(None, help="Filter by subject id, e.g. C-007"),
    delete: int | None = typer.Option(None, help="Delete the memory with this id"),
) -> None:
    """List (or delete) long-term memories."""
    from opspilot import memory as mem

    if delete is not None:
        mem.delete_memory(delete)
        console.print(f"Deleted memory {delete}")
    table = Table(title="Long-term memory")
    for col in ("id", "subject", "kind", "content", "session", "created"):
        table.add_column(col, overflow="fold")
    for m in mem.list_memories(subject):
        table.add_row(
            str(m["id"]),
            f"{m['subject_type']}:{m['subject_id']}",
            m["kind"],
            m["content"],
            m["source_session"] or "",
            m["created_at"],
        )
    console.print(table)


if __name__ == "__main__":
    app()
