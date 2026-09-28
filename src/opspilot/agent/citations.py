"""Readable labels for evidence IDs, e.g. policy:return_policy_v2§2 -> 'Return Policy v2 §2'."""

from __future__ import annotations

import re

from opspilot.agent.schemas import Evidence, RetrievalBundle
from opspilot.safety.grounding import extract_citations, normalize_brackets

_BRACKETS = re.compile(r"\[([^\[\]]+)\]")


def label_for(evidence_id: str, bundle: RetrievalBundle | None = None) -> str:
    """Human-readable reference for an evidence ID."""
    ev = bundle.get(evidence_id) if bundle else None
    kind, _, rest = evidence_id.partition(":")
    if kind == "policy":
        label = ev.metadata.get("label") if ev else None
        if not label:
            doc, _, section = rest.partition("§")
            label = f"{doc.replace('_', ' ').title()} §{section}"
        return f"{label} (superseded)" if ev and ev.superseded else label
    if kind == "db":
        table, _, key = rest.partition(":")
        return {
            "orders": f"Order {key}",
            "customers": f"Customer {key}",
            "products": f"Product record {key}",
        }.get(table, f"{table} {key}")
    if kind == "csv":
        return f"Warehouse feed {rest.partition(':')[2]}"
    if kind == "faq":
        return f"FAQ {rest}"
    if kind == "memory":
        return f"Memory #{rest}"
    return evidence_id


_TYPOGRAPHY = str.maketrans({"\u2010": "-", "\u2011": "-", "\u00a0": " ", "\u202f": " "})


def normalize_typography(text: str) -> str:
    """Models often write IDs with non-breaking hyphens (U+2011) or narrow no-break
    spaces; map them to plain ASCII so IDs stay searchable and copy-pasteable."""
    return text.translate(_TYPOGRAPHY)


def humanize(text: str, bundle: RetrievalBundle | None = None) -> str:
    """Replace [evidence_id, ...] citations with readable labels (and normalise typography)."""

    def repl(match: re.Match[str]) -> str:
        ids = extract_citations(match.group(0))
        if not ids:
            return match.group(0)
        return "[" + "; ".join(dict.fromkeys(label_for(i, bundle) for i in ids)) + "]"

    return _BRACKETS.sub(repl, normalize_brackets(normalize_typography(text or "")))


def cited_ids(decisions: list[dict], bundle: RetrievalBundle) -> list[str]:
    """Every evidence ID cited anywhere in the given decisions, in first-seen order."""
    found: list[str] = []
    for d in decisions:
        found += extract_citations(d.get("analysis")) + extract_citations(d.get("answer_draft"))
        for s in d.get("steps", []):
            found += s.get("citations", []) + extract_citations(s.get("rationale"))
    known = bundle.ids()
    return [i for i in dict.fromkeys(found) if i in known]


def evidence_view(ev: Evidence, bundle: RetrievalBundle) -> dict:
    """Evidence as a dict with its readable label (for UI/CLI)."""
    return {**ev.model_dump(), "label": label_for(ev.id, bundle)}
