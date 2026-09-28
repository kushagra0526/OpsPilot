"""Grounding validation and the relevance signal."""

from __future__ import annotations

from opspilot.agent.schemas import AgentDecision, Evidence, Intent, PlanStep, RetrievalBundle
from opspilot.safety.grounding import extract_citations, relevance_signal, validate_decision

V2 = "policy:return_policy_v2§2"
V1 = "policy:return_policy_v1§2"
ORDER = "db:orders:ORD-1007"


def _bundle() -> RetrievalBundle:
    return RetrievalBundle(
        evidence=[
            Evidence(id=V2, source_type="policy", content="14 days"),
            Evidence(id=V1, source_type="policy", content="30 days", superseded=True),
            Evidence(id=ORDER, source_type="db", content="order"),
        ],
        max_similarity=0.5,
        entity_hits=1,
    )


def _decision(steps: list[PlanStep], analysis: str = f"Eligible [{V2}].") -> AgentDecision:
    kind = "act" if steps else "answer"
    return AgentDecision(decision_type=kind, analysis=analysis, steps=steps, confidence=0.9)


def _refund(cites: list[str], **kw) -> PlanStep:
    return PlanStep(
        id=kw.get("id", "s1"),
        tool=kw.get("tool", "issue_refund"),
        args=kw.get("args", {"order_id": "ORD-1007", "amount": 149, "reason": "x"}),
        rationale="refund",
        citations=cites,
        depends_on=kw.get("depends_on", []),
    )


def test_extract_citations() -> None:
    text = f"See [{V2}, {ORDER}] and [not an id] [faq:faq-04]"
    assert extract_citations(text) == [V2, ORDER, "faq:faq-04"]


def test_valid_decision_passes() -> None:
    assert validate_decision(_decision([_refund([ORDER])]), _bundle()) == []


def test_unknown_citation_rejected() -> None:
    errs = validate_decision(_decision([_refund(["db:orders:ORD-9999"])]), _bundle())
    assert any("not in the EVIDENCE block" in e for e in errs)


def test_superseded_citation_as_basis_for_action_rejected() -> None:
    errs = validate_decision(_decision([_refund([V1])]), _bundle())
    assert any("SUPERSEDED" in e for e in errs)


def test_superseded_may_be_mentioned_in_analysis() -> None:
    d = _decision([], f"v2 applies [{V2}]; v1 [{V1}] is superseded.")
    assert validate_decision(d, _bundle()) == []


def test_uncited_analysis_rejected() -> None:
    assert validate_decision(_decision([], "It is 14 days."), _bundle())


def test_step_without_citation_rejected() -> None:
    assert any("must cite" in e for e in validate_decision(_decision([_refund([])]), _bundle()))


def test_unknown_tool_and_bad_args_rejected() -> None:
    errs = validate_decision(_decision([_refund([ORDER], tool="wire_money")]), _bundle())
    assert any("unknown tool" in e for e in errs)
    errs = validate_decision(_decision([_refund([ORDER], args={"order_id": "x"})]), _bundle())
    assert any("invalid args" in e for e in errs)


def test_cycle_rejected() -> None:
    steps = [
        _refund([ORDER], id="s1", depends_on=["s2"]),
        _refund([ORDER], id="s2", depends_on=["s1"]),
    ]
    assert any("cycle" in e for e in validate_decision(_decision(steps), _bundle()))


def test_relevance_signal_is_an_input_not_a_decision() -> None:
    off = RetrievalBundle(max_similarity=0.05)
    assert "likely unrelated" in relevance_signal(Intent(summary="weather"), off)
    assert "Signal: related" in relevance_signal(Intent(summary="x", order_ids=["ORD-1001"]), off)
    assert "Signal: related" in relevance_signal(
        Intent(summary="coupon"), RetrievalBundle(max_similarity=0.7)
    )


def test_fullwidth_brackets_are_citations() -> None:
    from opspilot.agent.citations import humanize

    assert extract_citations(f"see 【{V2}】") == [V2]
    assert humanize(f"window 【{V2}】 and 【s1】") == "window [Return Policy V2 §2] and [s1]"


def test_humanize_normalises_unicode_hyphens_and_spaces() -> None:
    from opspilot.agent.citations import humanize

    text = "Order ORD‑1001 for C‐001: 10 % coupon, ok now"
    assert humanize(text) == "Order ORD-1001 for C-001: 10 % coupon, ok now"
