"""Scenario evaluation: runs scenarios through AgentService and scores each check.

`--set main` (default) = SPEC §14 scenarios -> evals/results.md; `--set holdout` = held-out
scenarios -> evals/holdout_results.md. `--runs N` repeats the whole set and adds a stability
table. `--only` runs and runs stopped by
a daily quota write *_partial.md, so committed full-run results are never overwritten.
Every report records the git commit and whether the working tree was clean.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from opspilot import audit
from opspilot.config import ROOT_DIR, get_settings
from opspilot.seeding import seed_all, seed_database

EVAL_DIR = ROOT_DIR / "evals"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def run_scenario(svc: Any, sc: dict[str, Any]) -> dict[str, Any]:
    """Execute all turns of one scenario; collect what the checks need."""
    seed_database()
    session = f"eval-{sc['id']}-{uuid.uuid4().hex[:6]}"
    start = time.time()
    calls, tokens, approval_requested, out = 0, 0, False, None
    by_tier: dict[str, int] = {}
    for turn in sc["turns"]:
        if turn.get("new_session"):
            session = f"eval-{sc['id']}-{uuid.uuid4().hex[:6]}"
        out = svc.start_turn(session, turn["input"], "eval-operator")
        while out.status == "awaiting_approval":
            approval_requested = True
            answer = sc.get("approval_response") or {
                "approved": False,
                "approver": "eval",
                "note": "no approval scripted",
            }
            out = svc.resume_with_approval(
                session, {p["step_id"]: answer for p in out.pending_approvals}
            )
        usage = out.usage.get("turn", {})
        calls += usage.get("requests", 0)
        tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        for tier, n in (usage.get("tokens_by_tier") or {}).items():
            by_tier[tier] = by_tier.get(tier, 0) + n
    assert out is not None
    return {
        "out": out,
        "session": session,
        "latency": time.time() - start,
        "calls": calls,
        "tokens": tokens,
        "tokens_by_tier": by_tier,
        "approval_requested": approval_requested,
    }


def score(sc: dict[str, Any], run: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """Evaluate every configured check. Returns (check, passed, detail)."""
    out = run["out"]
    results = out.step_results
    executed = {r["tool"] for r in results if r["status"] == "executed"}
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    decision = (out.decision or {}).get("decision_type")
    add(
        "decision_type",
        decision in _as_list(sc["expected_decision_type"]),
        f"got {decision}, want {sc['expected_decision_type']}",
    )
    want = set(sc.get("expected_tools") or [])
    add("expected_tools", want <= executed, f"executed {sorted(executed)}, want ⊇ {sorted(want)}")
    add(
        "approval",
        run["approval_requested"] == sc["must_require_approval"],
        f"approval requested={run['approval_requested']}",
    )
    forbidden = set(sc.get("must_not_tools") or []) & executed
    add(
        "must_not_tools",
        not forbidden,
        f"forbidden executed: {sorted(forbidden)}" if forbidden else "none",
    )
    if sc.get("must_cite_doc_ids"):
        docs = {
            e["metadata"].get("doc_id")
            for e in out.evidence_used
            if e["source_type"] == "policy" and not e["superseded"]
        }
        missing = set(sc["must_cite_doc_ids"]) - docs
        add("citations", not missing, f"cited docs {sorted(d for d in docs if d)}")
    if sc.get("must_cite_source_types"):
        types = {e["source_type"] for e in out.evidence_used}
        add(
            "cited_source_types",
            set(sc["must_cite_source_types"]) <= types,
            f"cited types {sorted(types)}",
        )
    if sc.get("response_must_contain"):
        text = out.final_response or ""
        add(
            "response_text", all(s in text for s in sc["response_must_contain"]), "response content"
        )
    if sc.get("email_depends_on_refund"):
        steps = next(
            (
                h["decision"]["steps"]
                for h in reversed(out.plan_history)
                if any(s["tool"] == "issue_refund" for s in h["decision"]["steps"])
            ),
            [],
        )
        refund = {s["id"] for s in steps if s["tool"] == "issue_refund"}
        ok = any(
            s["tool"] == "send_customer_email" and refund & set(s["depends_on"]) for s in steps
        )
        add("email_after_refund", ok, "email step depends on refund step")
    if sc.get("max_coupon_percent") is not None:
        pcts = [
            r["args"].get("percent_off", 0)
            for r in results
            if r["tool"] == "issue_coupon" and r["status"] == "executed"
        ]
        add("coupon_limit", bool(pcts) and max(pcts) <= sc["max_coupon_percent"], f"coupons {pcts}")
    if sc.get("must_detect_conflict"):
        kinds = {c["kind"] for c in out.conflicts}
        add("conflict_detected", sc["must_detect_conflict"] in kinds, f"conflicts {sorted(kinds)}")
    if sc.get("must_audit_event"):
        events = {r["event"] for r in audit.get_audit(run["session"])}
        add("audit_event", sc["must_audit_event"] in events, f"events {sorted(events)}")
    refunded = {
        r["args"].get("order_id")
        for r in results
        if r["tool"] == "issue_refund" and r["status"] == "executed"
    }
    if sc.get("expected_refund_orders"):
        want_orders = set(sc["expected_refund_orders"])
        add("refunded_orders", want_orders <= refunded, f"refunded {sorted(refunded)}")
    if sc.get("must_not_refund_orders"):
        bad = set(sc["must_not_refund_orders"]) & refunded
        add("not_refunded_orders", not bad, f"wrongly refunded {sorted(bad)}" if bad else "none")
    if sc.get("expected_refund_method") or sc.get("expected_refund_amount"):
        refunds = [r for r in results if r["tool"] == "issue_refund" and r["status"] == "executed"]
        data = (refunds[0]["output"].get("data") or {}) if refunds else {}
        ok = bool(refunds)
        if sc.get("expected_refund_method"):
            ok = ok and data.get("method") == sc["expected_refund_method"]
        if sc.get("expected_refund_amount"):
            ok = ok and abs(float(data.get("amount", 0)) - sc["expected_refund_amount"]) < 0.01
        add("refund_details", ok, f"refund {data.get('amount')} via {data.get('method')}")
    return checks


def outcome_record(
    sc: dict[str, Any], run: dict[str, Any], checks: list[tuple[str, bool, str]]
) -> dict[str, Any]:
    """What actually happened in one scenario: decision, each step, approvals, final message."""
    out = run["out"]
    return {
        "id": sc["id"],
        "name": sc["name"],
        "prompts": [t["input"] for t in sc["turns"]],
        "decision_type": (out.decision or {}).get("decision_type"),
        "iterations": len(out.plan_history),
        "approval_requested": run["approval_requested"],
        "approval_response": sc.get("approval_response"),
        "steps": [
            {
                "iteration": r["iteration"],
                "step_id": r["step_id"],
                "tool": r["tool"],
                "status": r["status"],
                "summary": (r.get("output") or {}).get("summary") or r.get("error") or "",
            }
            for r in out.step_results
        ],
        "conflicts": [c["kind"] for c in out.conflicts],
        "final_response": out.final_response or "",
        "passed": all(ok for _, ok, _ in checks),
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
    }


def _git_state() -> str:
    """Short commit hash, plus a flag when code/data/eval files have uncommitted changes."""
    paths = [
        "src",
        "data/knowledge",
        "evals/scenarios.yaml",
        "evals/holdout.yaml",
        "evals/run_evals.py",
    ]
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            capture_output=True,
            text=True,
            cwd=ROOT_DIR,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"`{rev}`" + (" (with uncommitted changes)" if dirty else " (clean working tree)")


def _row_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| # | Scenario | Checks | Result | Latency (s) | LLM calls | Tokens | Failed checks |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        failed = "; ".join(f"{n} ({d})" for n, ok, d in r["checks"] if not ok) or "—"
        lines.append(
            f"| {r['id']} | {r['name']} | {r['passed']}/{r['total']} | "
            f"{'✅ pass' if r['passed'] == r['total'] else '❌ fail'} | {r['latency']:.1f} | "
            f"{r['calls']} | {r['tokens']:,} | {failed} |"
        )
    return lines


def _passes(rows: list[dict[str, Any]]) -> int:
    return sum(r["passed"] == r["total"] for r in rows)


def _stability(runs: list[list[dict[str, Any]]]) -> list[str]:
    first = runs[0]
    lines = [
        "",
        "### Stability across runs",
        "",
        "| # | Scenario | " + " | ".join(f"Run {k + 1}" for k in range(len(runs))) + " | Passed |",
        "|---|---|" + "---|" * len(runs) + "---|",
    ]
    for i, r in enumerate(first):
        marks = []
        for run in runs:
            row = run[i] if i < len(run) else None
            marks.append("—" if row is None else ("✅" if row["passed"] == row["total"] else "❌"))
        lines.append(
            f"| {r['id']} | {r['name']} | "
            + " | ".join(marks)
            + f" | {marks.count('✅')}/{len(runs)} |"
        )
    totals = " · ".join(f"Run {k + 1}: {_passes(run)}/{len(run)}" for k, run in enumerate(runs))
    lines += ["", f"**Per run:** {totals}"]
    for k, run in enumerate(runs[1:], start=2):
        failing = [r for r in run if r["passed"] != r["total"]]
        if failing:
            lines += ["", f"Run {k} failures:"]
            lines += [
                f"- {r['id']} {r['name']}: "
                + "; ".join(f"{n} ({d})" for n, ok, d in r["checks"] if not ok)
                for r in failing
            ]
    return lines


def tier_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Tokens a run spent, in total and per model tier (the tiers have separate daily quotas)."""
    large = sum((r.get("tokens_by_tier") or {}).get("large", 0) for r in rows)
    small = sum((r.get("tokens_by_tier") or {}).get("small", 0) for r in rows)
    return {
        "tokens_total": sum(r["tokens"] for r in rows),
        "tokens_large": large,
        "tokens_small": small,
    }


RUNS_DIR = EVAL_DIR / "runs"


def code_fingerprint(set_name: str) -> str:
    """Hash of everything that can change a scenario's outcome: agent code, knowledge files and
    the scenario definitions. Stored runs only combine when their fingerprints match."""
    digest = hashlib.sha256()
    files = sorted((ROOT_DIR / "src" / "opspilot").rglob("*.py"))
    files += sorted(p for p in (ROOT_DIR / "data" / "knowledge").rglob("*") if p.is_file())
    files.append(EVAL_DIR / ("scenarios.yaml" if set_name == "main" else "holdout.yaml"))
    for path in files:
        digest.update(path.relative_to(ROOT_DIR).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


def save_run(set_name: str, rows: list[dict[str, Any]], meta: dict[str, Any]) -> Path:
    """Persist one complete live run so runs made on different days can be combined."""
    RUNS_DIR.mkdir(exist_ok=True)
    stamp = meta["started"].replace(":", "").replace("-", "")
    path = RUNS_DIR / f"{set_name}-{stamp}.json"
    path.write_text(json.dumps({"meta": meta, "rows": rows}, indent=2) + "\n", encoding="utf-8")
    return path


def stored_runs(set_name: str, fingerprint: str) -> list[dict[str, Any]]:
    """Complete live runs of this set made on the current code, oldest first."""
    if not RUNS_DIR.exists():
        return []
    runs = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(RUNS_DIR.glob(f"{set_name}-*.json"))
    ]
    return sorted(
        (r for r in runs if r["meta"]["fingerprint"] == fingerprint),
        key=lambda r: r["meta"]["started"],
    )


def render_report(
    runs: list[list[dict[str, Any]]],
    *,
    title: str,
    mode: str,
    model: dict[str, str],
    started: str,
    git: str,
    planned: int,
    run_meta: list[dict[str, Any]] | None = None,
) -> str:
    """Markdown: header, run 1 in detail, and (if >1 run) a stability table over all runs."""
    first = runs[0]
    incomplete = "" if len(first) == planned else f" (incomplete: {len(first)} of {planned} ran)"
    lines = [
        f"## {title}",
        "",
        f"- Started: {started} · code: {git}",
        *[
            f"- Run {k + 1}: started {m['started'].replace('T', ' ')}, finished "
            f"{m['finished'].replace('T', ' ')} · code {m['git']} · fingerprint `{m['fingerprint']}`"
            for k, m in enumerate(run_meta or [])
        ],
        f"- Mode: **{mode}** · provider `{model.get('provider')}` · large `{model.get('large')}`"
        f" · small `{model.get('small')}`",
        f"- **Run 1: {_passes(first)}/{len(first)} scenarios fully passing**{incomplete}",
        *[
            f"- Tokens, run {k + 1}: {t['tokens_total']:,} total "
            f"(large model {t['tokens_large']:,}, small model {t['tokens_small']:,})"
            for k, t in enumerate(tier_totals(r) for r in runs)
        ],
        "",
        *_row_table(first),
    ]
    if len(runs) > 1:
        lines += _stability(runs)
    lines += [
        "",
        "Checks per scenario: decision type, expected tools executed, approval required (or not), "
        "forbidden tools never executed, plus scenario-specific checks (citations, conflict "
        "detection, audit events, refund orders/method/amount, coupon limits, email ordering).",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    """Run a scenario set one or more times and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=["main", "holdout"], default="main")
    parser.add_argument("--only", nargs="*", help="scenario ids to run (e.g. 3 7 or H2)")
    parser.add_argument("--runs", type=int, default=1, help="repeat the whole set N times")
    parser.add_argument("--label", default=None, help="section title, e.g. 'before fixes'")
    parser.add_argument("--append", action="store_true", help="append a section to the report")
    parser.add_argument("--throttle", type=float, default=None, help="seconds between cases")
    args = parser.parse_args()
    if not get_settings().groq_api_key:
        print(
            "GROQ_API_KEY is not set: add it to .env (free at console.groq.com) and rerun "
            "`make eval`."
        )
        return 1
    from opspilot.llm import DailyLimitError
    from opspilot.service import AgentService

    throttle = args.throttle if args.throttle is not None else 20.0
    source = "scenarios.yaml" if args.set == "main" else "holdout.yaml"
    scenarios = yaml.safe_load((EVAL_DIR / source).read_text(encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if str(s["id"]) in args.only]
    base = "results" if args.set == "main" else "holdout_results"
    suffix = "_partial" if args.only else ""
    out_path = EVAL_DIR / f"{base}{suffix}.md"
    default_title = "Main scenarios" if args.set == "main" else "Held-out scenarios"
    title = args.label or default_title
    started, git = datetime.now().isoformat(timespec="seconds"), _git_state()
    seed_all()
    svc = AgentService()
    runs: list[list[dict[str, Any]]] = []
    outcomes: list[dict[str, Any]] = []
    stopped = False
    limit_hit: dict[str, Any] | None = None
    fingerprint = code_fingerprint(args.set)
    run_times: list[tuple[str, str]] = []
    for k in range(args.runs):
        rows: list[dict[str, Any]] = []
        runs.append(rows)
        run_started = datetime.now().isoformat(timespec="seconds")
        print(f"=== run {k + 1}/{args.runs} ===")
        for i, sc in enumerate(scenarios):
            if (i or k) and throttle:
                time.sleep(throttle)
            try:
                run = run_scenario(svc, sc)
                checks = score(sc, run)
                latency, calls, tokens = run["latency"], run["calls"], run["tokens"]
                by_tier = run["tokens_by_tier"]
                if k == 0:
                    outcomes.append(outcome_record(sc, run, checks))
            except DailyLimitError as exc:
                print(f"DAILY LIMIT REACHED at run {k + 1}, scenario {sc['id']}: {exc}")
                used = re.search(r"Limit (\d+), Used (\d+)", str(exc))
                org = re.search(r"organization `([^`]+)`", str(exc))
                limit_hit = {
                    "org": org.group(1) if org else None,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "limit": int(used.group(1)) if used else None,
                    "used": int(used.group(2)) if used else None,
                }
                stopped = True
                break
            except Exception as exc:  # a crashing case is a failed case, not a crashed eval
                checks = [("run", False, f"{type(exc).__name__}: {exc}")]
                latency, calls, tokens, by_tier = 0.0, 0, 0, {}
            ok = sum(c[1] for c in checks)
            rows.append(
                {
                    "id": sc["id"],
                    "name": sc["name"],
                    "checks": checks,
                    "passed": ok,
                    "total": len(checks),
                    "latency": latency,
                    "calls": calls,
                    "tokens": tokens,
                    "tokens_by_tier": by_tier,
                }
            )
            print(
                f"[{sc['id']:>3}] {sc['name']:<48} {ok}/{len(checks)}"
                + "".join(f"\n      ✗ {n}: {d}" for n, good, d in checks if not good)
            )
        if stopped:
            break
        run_times.append((run_started, datetime.now().isoformat(timespec="seconds")))
    runs = [r for r in runs if r]
    if not runs:
        return 3
    if stopped:
        out_path = EVAL_DIR / f"{base}_partial.md"
        title += " (stopped by daily quota)"
    report = render_report(
        runs,
        title=title,
        mode="live",
        model=svc.llm.describe(),
        started=started,
        git=git,
        planned=len(scenarios),
    )
    header = (
        "# OpsPilot evaluation results\n\n"
        if args.set == "main"
        else "# OpsPilot held-out evaluation\n\n"
    )
    complete_live = not (stopped or args.only)
    if complete_live:
        for rows, (run_started, run_finished) in zip(runs, run_times, strict=True):
            meta = {
                "set": args.set,
                "started": run_started,
                "finished": run_finished,
                "git": git,
                "fingerprint": fingerprint,
                "model": svc.llm.describe(),
                **tier_totals(rows),
            }
            print(f"saved {save_run(args.set, rows, meta).relative_to(ROOT_DIR)}")
    if complete_live and args.set == "main":
        # results.md always shows every complete live run on the current code, oldest first.
        stored = stored_runs("main", fingerprint)
        report = render_report(
            [r["rows"] for r in stored],
            title=title,
            mode="live",
            model=stored[0]["meta"]["model"],
            started=stored[0]["meta"]["started"],
            git=stored[0]["meta"]["git"],
            planned=len(scenarios),
            run_meta=[r["meta"] for r in stored],
        )
        out_path.write_text(header + report, encoding="utf-8")
    elif args.append and out_path.exists():
        existing = out_path.read_text(encoding="utf-8").rstrip("\n")
        out_path.write_text(existing + "\n\n" + report, encoding="utf-8")
    else:
        out_path.write_text(header + report, encoding="utf-8")
    print(f"wrote {out_path.relative_to(ROOT_DIR)}")
    ledger = {
        "started": started,
        "finished": datetime.now().isoformat(timespec="seconds"),
        "set": args.set,
        "label": title,
        "mode": "live",
        "model": svc.llm.describe(),
        "code": git,
        "runs": [{"scenarios": len(r), "passed": _passes(r), **tier_totals(r)} for r in runs],
        "stopped_by_daily_limit": limit_hit,
        # Which key's quota paid for this run (short SHA-256, never the key itself).
        "key": hashlib.sha256((get_settings().groq_api_key or "").encode()).hexdigest()[:10],
    }
    with (EVAL_DIR / "token_ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ledger) + "\n")
    if outcomes and not args.only:
        out_json = EVAL_DIR / f"outcomes_{args.set}{'_partial' if stopped else ''}.json"
        meta = {
            "started": started,
            "git": git,
            "mode": "live",
            "model": svc.llm.describe(),
        }
        payload = json.dumps({"meta": meta, "scenarios": outcomes}, indent=2)
        out_json.write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {out_json.relative_to(ROOT_DIR)}")
    return 3 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
