"""OpsPilot Streamlit UI: agent chat with reasoning/plan/approvals/execution/sources, audit, memory, data."""

from __future__ import annotations

import html
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yaml

from opspilot import db
from opspilot.agent.citations import humanize
from opspilot.agent.schemas import Evidence, RetrievalBundle
from opspilot.config import ROOT_DIR, get_settings
from opspilot.llm import DailyLimitError, LLMError, MissingAPIKeyError
from opspilot.seeding import seed_all
from opspilot.service import AgentService, PendingApprovalError

STATIC = Path(__file__).parent / "static"
st.set_page_config(
    page_title="OpsPilot: NovaMart support operations agent",
    page_icon=str(STATIC / "mark-64.png"),
    layout="wide",
)

# (text colour, background) pairs; muted and all WCAG AA on their background.
RISK_STYLE = {
    "low": ("#3F6147", "#E2EADF"),
    "medium": ("#6E510F", "#F2E7CC"),
    "high": ("#8A3A2F", "#F1DDD7"),
}
STATUS_STYLE = {
    "executed": ("#3F6147", "#E2EADF"),
    "failed": ("#8A3A2F", "#F1DDD7"),
    "rejected": ("#8A3A2F", "#F1DDD7"),
    "skipped": ("#4A4740", "#E8E4DA"),
}
NEUTRAL = ("#4A4740", "#E8E4DA")
SCENARIOS_PATH = ROOT_DIR / "evals" / "scenarios.yaml"
DATA_TABLES = [
    "orders",
    "refunds",
    "coupons",
    "tickets",
    "outbox",
    "customers",
    "products",
    "order_items",
]
MARK_SVG = (STATIC / "mark.svg").read_text(encoding="utf-8")

st.markdown(
    """
<style>
:root { --ink:#1C1B19; --ink-2:#4A4740; --paper:#F6F4EF; --paper-2:#EEEAE1; --rule:#D9D3C6;
        --accent:#2E5E4E; }
* { box-shadow: none !important; }
[data-testid="stStatusWidget"] { visibility: hidden; }
h1, h2, h3, h4 { letter-spacing: -0.005em; }
.brand { display:flex; align-items:center; gap:.6rem; margin:.25rem 0 .1rem; }
.brand svg { width:28px; height:28px; flex:none; }
.brand span { font-family:"Newsreader", serif; font-size:1.55rem; font-weight:500; color:var(--ink); }
.tag { display:inline-block; padding:1px 6px; border:1px solid currentColor; border-radius:2px;
       font-family:"IBM Plex Mono", monospace; font-size:.72rem; font-weight:500;
       letter-spacing:.03em; text-transform:uppercase; white-space:nowrap; }
.mono { font-family:"IBM Plex Mono", monospace; font-size:.82rem; }
.small { font-size:.82rem; color:var(--ink-2); }
table.plan { border-collapse:collapse; width:100%; font-size:.86rem; }
table.plan th { text-align:left; font-weight:600; border-bottom:1px solid var(--ink);
                padding:.35rem .5rem; }
table.plan td { border-bottom:1px solid var(--rule); padding:.45rem .5rem; vertical-align:top; }
.ev { border:1px solid var(--rule); border-radius:2px; padding:.5rem .7rem; margin:.4rem 0;
      font-size:.86rem; background:var(--paper); }
.ev .id { font-family:"IBM Plex Mono", monospace; font-size:.75rem; color:var(--ink-2); }
.ev.superseded .body { text-decoration:line-through; color:var(--ink-2); }
.track { position:relative; height:10px; border:1px solid var(--rule); background:var(--paper); }
.span { position:absolute; top:0; bottom:0; background:var(--accent); }
.progress { border:1px solid var(--rule); border-radius:2px; padding:.6rem .8rem;
            background:var(--paper-2); font-size:.9rem; }
.progress div { padding:.1rem 0; }
.progress .state { display:inline-block; width:3.2rem; font-family:"IBM Plex Mono", monospace;
                   font-size:.75rem; text-transform:uppercase; color:var(--ink-2); }
.progress .now { color:var(--ink); font-weight:600; }
.progress .now .state { color:var(--accent); }
/* Framework icons (Material Symbols / SVG glyphs) replaced with text labels. */
[data-testid="stExpander"] summary [data-testid="stIconMaterial"] { font-size:0 !important;
  width:1.2rem; overflow:hidden; }
[data-testid="stExpander"] summary [data-testid="stIconMaterial"]::before { content:"+";
  font:500 1rem "IBM Plex Mono", monospace; }
[data-testid="stExpander"] details[open] > summary [data-testid="stIconMaterial"]::before {
  content:"-"; }
[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"],
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] { font-size:0 !important; }
[data-testid="stSidebarCollapseButton"] button::after { content:"Hide sidebar";
  font:400 .78rem "Public Sans", sans-serif; }
[data-testid="stExpandSidebarButton"]::after { content:"Show sidebar";
  font:400 .78rem "Public Sans", sans-serif; }
[data-testid="stChatInputSubmitButton"] svg { display:none; }
[data-testid="stChatInputSubmitButton"]::after { content:"Send"; font:600 .8rem "Public Sans", sans-serif; }
[data-testid="stChatInputSubmitButton"] { width:auto !important; padding:0 .6rem !important; }
[data-testid="stElementToolbar"] { display:none; }
</style>
""",
    unsafe_allow_html=True,
)


HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def md(text: str | None) -> str:
    """Chat markdown: amounts aren't LaTeX; model headings become bold lines, not page titles."""
    return HEADING_RE.sub(r"**\1**", (text or "").replace("$", r"\$"))


def cited(text: str, out: dict[str, Any]) -> str:
    """Render [evidence_id] citations as readable references using the turn's evidence."""
    bundle = RetrievalBundle(
        evidence=[
            Evidence.model_validate({k: v for k, v in e.items() if k != "label"})
            for e in out.get("evidence_used", [])
        ]
    )
    return humanize(text, bundle)


def esc(text: str) -> str:
    """HTML-escape for custom markup; `$` becomes an entity so Streamlit can't read it as LaTeX."""
    return html.escape(text).replace("$", "&#36;")


def tag(text: str, style: tuple[str, str] = NEUTRAL) -> str:
    """Small bordered label (risk level, step status, memory kind)."""
    fg, bg = style
    return f'<span class="tag" style="color:{fg};background:{bg}">{esc(text)}</span>'


class ProgressPanel:
    """Static, specific loading state: finished steps and the step running now (no animation)."""

    def __init__(self, first: str) -> None:
        self.slot = st.empty()
        self.lines: list[str] = []
        self.update("", first)

    def update(self, _node: str, message: str) -> None:
        """Record the step that just started and redraw."""
        self.lines.append(message)
        rows = [
            f'<div class="{"now" if i == len(self.lines) - 1 else ""}">'
            f'<span class="state">{"now" if i == len(self.lines) - 1 else "done"}</span>'
            f"{esc(line)}</div>"
            for i, line in enumerate(self.lines)
        ]
        self.slot.markdown(f'<div class="progress">{"".join(rows)}</div>', unsafe_allow_html=True)

    def clear(self) -> None:
        """Remove the panel once the turn is stored."""
        self.slot.empty()


@st.cache_resource(show_spinner=False)
def get_service() -> AgentService:
    """One agent service per app process (live Groq model)."""
    if not get_settings().chroma_dir.exists() or not db.db_path().exists():
        seed_all()
    return AgentService()


def load_scenarios() -> list[dict[str, Any]]:
    """Quick-start prompts from the eval scenario file."""
    return yaml.safe_load(Path(SCENARIOS_PATH).read_text(encoding="utf-8"))


def init_state() -> None:
    """Browser-scoped UI state (turn history itself lives in SQLite via the service)."""
    ss = st.session_state
    ss.setdefault("sessions", [])
    ss.setdefault("current", None)
    ss.setdefault("queued_prompt", None)
    ss.setdefault("operator", "operator-1")
    ss.setdefault("notice", None)


def restore_sessions(service: AgentService | None) -> None:
    """On a fresh page load, reopen stored sessions (newest first) instead of starting empty."""
    ss = st.session_state
    if ss.current is not None:
        return
    stored = service.list_sessions() if service else []
    if stored:
        ss.sessions, ss.current = stored, stored[0]
    else:
        new_session()


def new_session() -> None:
    """Create and select a fresh agent session."""
    sid = f"sess-{uuid.uuid4().hex[:6]}"
    st.session_state.sessions.insert(0, sid)
    st.session_state.current = sid


def history(service: AgentService | None) -> list[dict[str, Any]]:
    """Persisted turns of the current session."""
    return service.session_history(st.session_state.current) if service else []


# ---------------------------------------------------------------------------------------------
def sidebar(service: AgentService | None) -> None:
    """Session selector, operator, reset, model info, usage and quick-start prompts."""
    ss = st.session_state
    with st.sidebar:
        st.markdown(
            f'<div class="brand">{MARK_SVG}<span>OpsPilot</span></div>', unsafe_allow_html=True
        )
        st.caption("Support operations agent for NovaMart, a fictional store")
        if ss.current not in ss.sessions:
            ss.sessions.insert(0, ss.current)
        ids = list(ss.sessions)
        ss.current = st.selectbox("Session", ids, index=ids.index(ss.current))
        c1, c2 = st.columns(2)
        if c1.button("New session", width="stretch"):
            new_session()
            st.rerun()
        if c2.button(
            "Reset data",
            width="stretch",
            help="Rebuild SQLite and Chroma from the knowledge files",
        ):
            note = st.empty()
            note.caption("Rebuilding the database and policy index from data/knowledge")
            seed_all()
            note.empty()
            ss.sessions = []
            ss.notice = "Sample data was rebuilt from the knowledge files."
            new_session()
            st.rerun()
        st.text_input("Operator name", key="operator")
        if service:
            info = service.llm.describe()
            st.markdown(
                f"**Provider:** {info['provider']}  \n**Large model:** `{info['large']}`  \n"
                f"**Small model:** `{info['small']}`"
                + (f"  \n**Fallback:** {info['fallback']}" if info.get("fallback") else "")
            )
            turns = history(service)
            last = turns[-1]["outcome"] if turns else None
            if last:
                t, s = last["usage"].get("turn", {}), last["usage"].get("session", {})
                c1, c2 = st.columns(2)
                c1.metric("Turn LLM calls", t.get("requests", 0))
                c2.metric("Session calls", s.get("requests", 0))
                c1.metric("Turn tokens", t.get("input_tokens", 0) + t.get("output_tokens", 0))
                c2.metric("Retries", s.get("retries", 0))
        st.divider()
        st.markdown("**Example scenarios**")
        for sc in load_scenarios():
            for i, turn in enumerate(sc["turns"]):
                label = f"{sc['id']}{'AB'[i] if len(sc['turns']) > 1 else ''}. {sc['name']}"
                if st.button(label, key=f"qs-{sc['id']}-{i}", help=turn["input"], width="stretch"):
                    if turn.get("new_session"):
                        new_session()
                    ss.queued_prompt = turn["input"]
                    st.rerun()


# ---------------------------------------------------------------------------------------------
def render_reasoning(out: dict[str, Any]) -> None:
    """Analysis per iteration, conflicts, missing info."""
    for h in out["plan_history"]:
        d = h["decision"]
        st.markdown(
            f"**Iteration {h['iteration']}** · decision `{d['decision_type']}` · "
            f"confidence {d['confidence']:.2f}"
        )
        st.markdown(md(cited(d["analysis"], out)))
        for c in d.get("conflicts_considered") or []:
            st.markdown(md(f"- Conflict handled: {c}"))
    for c in out["conflicts"]:
        st.warning(
            md(f"**{c['kind'].replace('_', ' ')} conflict.** {c['detail']}  \n{c['resolution']}")
        )
    for m in out["missing"]:
        st.info(md(f"Missing: {m}"))


def render_plan(out: dict[str, Any]) -> None:
    """Plan table per iteration with risk labels."""
    risks = out["risk_assessments"]
    for h in out["plan_history"]:
        steps = h["decision"].get("steps") or []
        if not steps:
            continue
        st.markdown(f"**Iteration {h['iteration']}**")
        rows = [
            '<table class="plan"><tr><th>Step</th><th>Tool</th><th>Arguments</th><th>Risk</th>'
            "<th>After</th><th>Rationale</th></tr>"
        ]
        last = h is out["plan_history"][-1]
        for s in steps:
            r = risks.get(s["id"]) if last else None
            risk = (
                tag(
                    r["level"] + (" · approval" if r["requires_approval"] else ""),
                    RISK_STYLE[r["level"]],
                )
                if r
                else ""
            )
            rows.append(
                f"<tr><td class='mono'>{s['id']}</td><td class='mono'>{s['tool']}</td>"
                f"<td class='mono'>{esc(json.dumps(s['args'], ensure_ascii=False))}</td><td>{risk}</td>"
                f"<td class='mono'>{', '.join(s['depends_on']) or 'none'}</td>"
                f"<td class='small'>{esc(cited(s['rationale'], out))}</td></tr>"
            )
        st.markdown("".join(rows) + "</table>", unsafe_allow_html=True)
        if last:
            for s in steps:
                r = risks.get(s["id"])
                if r:
                    st.caption(md(f"{s['id']} risk: " + "; ".join(r["reasons"])))


def render_approvals(out: dict[str, Any], key: str) -> None:
    """One card per pending step with approve/reject + note; submit resumes the graph."""
    st.markdown("**Approval required.** These steps run only after a person approves them.")
    decisions: dict[str, dict[str, Any]] = {}
    for p in out["pending_approvals"]:
        with st.container(border=True):
            st.markdown(
                f"{tag(p['risk_level'], RISK_STYLE[p['risk_level']])} "
                f"<span class='mono'>{p['step_id']} · {p['tool']} · "
                f"{esc(json.dumps(p['args'], ensure_ascii=False))}</span>",
                unsafe_allow_html=True,
            )
            for reason in p["reasons"]:
                st.markdown(md(f"- {reason}"))
            st.caption(md(cited(p["rationale"], out)))
            choice = st.radio(
                "Decision",
                ["Approve", "Reject"],
                key=f"{key}-{p['step_id']}-d",
                horizontal=True,
                index=None,
            )
            note = st.text_input("Note", key=f"{key}-{p['step_id']}-n")
            decisions[p["step_id"]] = {"choice": choice, "note": note}
    if st.button("Submit decisions", key=f"{key}-submit", type="primary"):
        if any(v["choice"] is None for v in decisions.values()):
            st.error("Choose Approve or Reject for every step.")
            return
        payload = {
            sid: {
                "approved": v["choice"] == "Approve",
                "approver": st.session_state.get("operator") or "operator-1",
                "note": v["note"],
            }
            for sid, v in decisions.items()
        }
        run_resume(payload)


def render_execution(out: dict[str, Any]) -> None:
    """Results with status labels and a timing track so parallel steps visibly overlap."""
    results = list(out["step_results"])
    if not results:
        st.caption("No steps executed.")
        return
    timed = [r for r in results if r.get("started_at") and r.get("finished_at")]
    t0 = min((r["started_at"] for r in timed), default=0)
    span = max((r["finished_at"] for r in timed), default=1) - t0 or 1
    for r in results:
        cols = st.columns([1.2, 2.2, 5, 3])
        cols[0].markdown(
            tag(r["status"], STATUS_STYLE.get(r["status"], NEUTRAL)), unsafe_allow_html=True
        )
        cols[1].markdown(
            f"<span class='mono'>{r['step_id']} · iteration {r['iteration']}<br>{r['tool']}</span>",
            unsafe_allow_html=True,
        )
        summary = (r.get("output") or {}).get("summary") or r.get("error") or ""
        cols[2].markdown(f"<span class='small'>{esc(summary)}</span>", unsafe_allow_html=True)
        if r.get("started_at") and r.get("finished_at"):
            left = (r["started_at"] - t0) / span * 100
            width = max(2.0, (r["finished_at"] - r["started_at"]) / span * 100)
            ms = (r["finished_at"] - r["started_at"]) * 1000
            cols[3].markdown(
                f"<div class='track'><div class='span' style='left:{left:.1f}%;width:{width:.1f}%'>"
                f"</div></div><span class='small mono'>start +{(r['started_at'] - t0) * 1000:.0f} ms,"
                f" took {ms:.0f} ms</span>",
                unsafe_allow_html=True,
            )
    with st.expander("Raw step results"):
        st.json(results)


def render_sources(out: dict[str, Any]) -> None:
    """Cited evidence; superseded items struck through and labelled."""
    for e in out["evidence_used"]:
        cls = "ev superseded" if e["superseded"] else "ev"
        label = tag("superseded, not applied", RISK_STYLE["high"]) if e["superseded"] else ""
        st.markdown(
            f"<div class='{cls}'><b>{esc(e['label'])}</b> {label} "
            f"<span class='id'>{esc(e['id'])}</span>"
            f"<div class='body'>{esc(e['content'][:400])}</div></div>",
            unsafe_allow_html=True,
        )


def render_turn(turn: dict[str, Any], idx: int) -> None:
    """One chat exchange with all agent sections in SPEC order."""
    out = turn["outcome"]
    with st.chat_message("user", avatar=str(STATIC / "operator-64.png")):
        st.markdown(md(turn["input"]))
    with st.chat_message("assistant", avatar=str(STATIC / "mark-64.png")):
        with st.expander("Reasoning", expanded=False):
            render_reasoning(out)
        if any(h["decision"].get("steps") for h in out["plan_history"]):
            with st.expander("Plan", expanded=out["status"] == "awaiting_approval"):
                render_plan(out)
        if out["status"] == "awaiting_approval":
            with st.expander("Approvals", expanded=True):
                render_approvals(out, f"t{idx}")
        if out["step_results"]:
            with st.expander("Execution", expanded=True):
                render_execution(out)
        if out["evidence_used"]:
            with st.expander("Sources", expanded=False):
                render_sources(out)
        with st.expander("Trace", expanded=False):
            for t in out["trace"]:
                st.markdown(md(f"`{t['ts'][11:23]}` **{t['node']}**: {t['summary']}"))
        if out["status"] == "awaiting_approval":
            st.info("Waiting for approval. Decide on each step above to continue.")
        else:
            st.markdown(md(out["final_response"]))


# ---------------------------------------------------------------------------------------------
def _quota_message(exc: Exception) -> str:
    return (
        "The LLM provider's free-tier daily token quota is used up, so this request was not "
        f"processed. It refills over the day. Details: {exc}"
    )


def run_prompt(prompt: str) -> None:
    """Start a turn with a step-by-step progress panel; the service stores the outcome."""
    service = get_service()
    ss = st.session_state
    panel = ProgressPanel("Starting the turn")
    try:
        service.start_turn(
            ss.current, prompt, ss.get("operator") or "operator-1", on_progress=panel.update
        )
    except PendingApprovalError as exc:
        panel.clear()
        st.warning(str(exc))
        return
    except DailyLimitError as exc:
        panel.clear()
        st.error(_quota_message(exc))
        return
    except LLMError as exc:
        panel.clear()
        st.error(f"The language model did not respond after several retries. {exc}")
        return
    st.rerun()  # the service persisted the turn; the rerun renders it from SQLite


def run_resume(payload: dict[str, dict[str, Any]]) -> None:
    """Resume the interrupted turn with the operator's decisions."""
    service = get_service()
    panel = ProgressPanel("Submitting approval decisions")
    try:
        service.resume_with_approval(st.session_state.current, payload, on_progress=panel.update)
    except DailyLimitError as exc:
        panel.clear()
        st.error(_quota_message(exc))
        return
    except LLMError as exc:
        panel.clear()
        st.error(f"The language model did not respond after several retries. {exc}")
        return
    st.rerun()


def agent_tab(service: AgentService) -> None:
    """Chat history + input."""
    ss = st.session_state
    turns = history(service)
    if ss.notice:
        st.success(ss.notice)
        ss.notice = None
    if not turns:
        st.markdown("#### Describe a support request")
        st.caption(
            "Or pick an example scenario in the sidebar, for example: Process a full refund for "
            "ORD-1001, the laptop has a dead pixel."
        )
    for i, turn in enumerate(turns):
        render_turn(turn, i)
    waiting = bool(turns) and turns[-1]["outcome"]["status"] == "awaiting_approval"
    prompt = st.chat_input("Describe the customer request", disabled=waiting)
    queued = ss.queued_prompt
    if queued and not waiting:
        ss.queued_prompt = None
        run_prompt(queued)
    elif prompt:
        run_prompt(prompt)


def audit_tab(service: AgentService) -> None:
    """Filterable audit log with CSV download."""
    rows = service.get_audit()
    if not rows:
        st.caption("No audit events yet.")
        return
    df = pd.DataFrame(rows)
    c = st.columns(4)
    filters = {
        "session_id": c[0].multiselect("Session", sorted(df.session_id.dropna().unique())),
        "event": c[1].multiselect("Event", sorted(df.event.unique())),
        "tool": c[2].multiselect("Tool", sorted(df.tool.dropna().unique())),
        "risk_level": c[3].multiselect("Risk", sorted(df.risk_level.dropna().unique())),
    }
    for col, values in filters.items():
        if values:
            df = df[df[col].isin(values)]
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_order=[
            "id",
            "ts",
            "session_id",
            "turn_id",
            "event",
            "tool",
            "risk_level",
            "actor",
            "rationale",
            "args_json",
            "result_json",
        ],
    )
    st.download_button(
        "Download audit log as CSV", df.to_csv(index=False), "opspilot_audit.csv", "text/csv"
    )


def memory_tab(service: AgentService) -> None:
    """Long-term memories with per-row delete."""
    mems = service.get_memories()
    if not mems:
        st.caption("No long-term memories yet. Try example scenario 10A.")
        return
    for m in mems:
        cols = st.columns([2, 2, 6, 2, 1])
        cols[0].markdown(f"**{m['subject_type']}** `{m['subject_id']}`")
        cols[1].markdown(tag(m["kind"].replace("_", " ")), unsafe_allow_html=True)
        cols[2].markdown(md(m["content"]))
        cols[3].caption(f"{m['created_at']} · {m['source_session']}")
        if cols[4].button("Delete", key=f"del-{m['id']}", help="Delete this memory"):
            service.delete_memory(m["id"])
            st.rerun()


def data_tab() -> None:
    """Browse business tables to show actions really changed data."""
    table = st.selectbox("Table", DATA_TABLES)
    rows = db.query(f"SELECT * FROM {table} ORDER BY rowid DESC")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(f"{len(rows)} rows")


def setup_message(exc: Exception) -> None:
    """Guidance when no LLM key is configured."""
    st.warning("**No LLM API key configured.** OpsPilot uses the free Groq API.")
    st.markdown(
        "1. Get a free key at [console.groq.com](https://console.groq.com/keys). No credit card "
        "is needed.\n"
        "2. Copy `.env.example` to `.env` and set `GROQ_API_KEY`.\n"
        "3. Restart `make run`."
    )
    st.caption(str(exc))


def main() -> None:
    """Page entry point."""
    init_state()
    service: AgentService | None
    error: Exception | None = None
    boot = st.empty()
    boot.caption("Starting OpsPilot: opening the database and the policy index")
    try:
        service = get_service()
    except MissingAPIKeyError as exc:
        service, error = None, exc
    boot.empty()
    restore_sessions(service)
    sidebar(service)
    tabs = st.tabs(["Agent", "Audit log", "Memory", "Knowledge and data"])
    with tabs[0]:
        if service is None:
            setup_message(error or RuntimeError("unavailable"))
        else:
            agent_tab(service)
    with tabs[1]:
        if service:
            audit_tab(service)
    with tabs[2]:
        if service:
            memory_tab(service)
    with tabs[3]:
        data_tab()


main()
