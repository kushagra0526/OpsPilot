"""Headless Streamlit UI tests (streamlit.testing AppTest) with the offline scripted planner."""

from __future__ import annotations

import pytest
from scripted_llm import ScriptedScenarioLLM
from streamlit.testing.v1 import AppTest

from opspilot import db
from opspilot.config import ROOT_DIR
from opspilot.llm import set_llm

APP = str(ROOT_DIR / "app" / "streamlit_app.py")


@pytest.fixture()
def app(fresh_db) -> AppTest:
    set_llm(ScriptedScenarioLLM())
    try:
        at = AppTest.from_file(APP, default_timeout=120)
        at.run()
        assert not at.exception, at.exception
        yield at
    finally:
        set_llm(None)


def _last_outcome(at: AppTest) -> dict:
    from opspilot.service import AgentService

    turns = AgentService.session_history(at.session_state["current"])
    return turns[-1]["outcome"]


def test_initial_render_has_all_tabs(app: AppTest) -> None:
    labels = [t.label for t in app.tabs]
    assert labels == ["Agent", "Audit log", "Memory", "Knowledge and data"]
    assert any("Example scenarios" in m.value for m in app.sidebar.markdown)


def test_quick_start_small_refund(app: AppTest) -> None:
    app.sidebar.button(key="qs-2-0").click().run()
    assert not app.exception, app.exception
    out = _last_outcome(app)
    assert out["status"] == "completed"
    assert {r["tool"] for r in out["step_results"]} == {"issue_refund", "send_customer_email"}
    assert any("Refunded" in e.value and "149.00" in e.value for e in app.markdown), "execution"


def test_approval_buttons_resume_the_turn(app: AppTest) -> None:
    app.sidebar.button(key="qs-3-0").click().run()
    assert _last_outcome(app)["status"] == "awaiting_approval"
    assert app.chat_input[0].disabled, "chat is locked while approval is pending"
    app.radio(key="t0-s1-d").set_value("Approve")
    app.text_input(key="t0-s1-n").input("checked photos")
    app.button(key="t0-submit").click().run()
    assert not app.exception, app.exception
    out = _last_outcome(app)
    assert out["status"] == "completed"
    refund = db.query_one("SELECT approved_by FROM refunds WHERE order_id='ORD-1001'")
    assert refund == {"approved_by": "operator-1"}


def test_reject_via_ui_never_executes(app: AppTest) -> None:
    app.sidebar.button(key="qs-9-0").click().run()
    app.radio(key="t0-s1-d").set_value("Reject")
    app.button(key="t0-submit").click().run()
    out = _last_outcome(app)
    assert {r["step_id"]: r["status"] for r in out["step_results"]} == {
        "s1": "rejected",
        "s2": "skipped",
    }
    assert db.query_one("SELECT status FROM orders WHERE id='ORD-1008'")["status"] == "placed"


def test_chat_input_and_refusal(app: AppTest) -> None:
    app.chat_input[0].set_value("What's the weather in Mumbai today?").run()
    assert not app.exception, app.exception
    assert _last_outcome(app)["decision"]["decision_type"] == "refuse"


def test_memory_tab_lists_and_deletes(app: AppTest) -> None:
    app.sidebar.button(key="qs-10-0").click().run()
    from opspilot.memory import list_memories

    mems = list_memories("C-007")
    assert any(m["kind"] == "preference" for m in mems)
    app.button(key=f"del-{mems[0]['id']}").click().run()
    assert len(list_memories("C-007")) == len(mems) - 1


def test_history_survives_a_page_reload(app: AppTest) -> None:
    app.sidebar.button(key="qs-2-0").click().run()
    session = app.session_state["current"]
    reloaded = AppTest.from_file(APP, default_timeout=120)
    reloaded.run()
    assert not reloaded.exception, reloaded.exception
    assert reloaded.session_state["current"] == session, "most recent session is reopened"
    assert any("Refunded" in e.value and "149.00" in e.value for e in reloaded.markdown), "restored"
    assert any(e.label.endswith("Reasoning") for e in reloaded.expander)
