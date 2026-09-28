"""Inventory: DB vs CSV conflict detection resolved by freshness."""

from __future__ import annotations

from opspilot.agent.schemas import Intent
from opspilot.knowledge.inventory import get_stock
from opspilot.knowledge.retriever import retrieve


def test_el_hp_02_conflict_resolved_to_csv() -> None:
    view = get_stock("EL-HP-02")
    assert view is not None
    assert view["db_stock"] == 5 and view["csv_stock"] == 0
    assert view["resolved_source"] == "csv" and view["resolved_stock"] == 0
    note = view["conflict"]
    assert note is not None and note.kind == "inventory"
    assert "Using CSV feed (snapshot 2026-09-26T18:00)" in note.resolution
    assert "fresher than DB (2026-09-20" in note.resolution


def test_agreeing_sources_have_no_conflict_and_db_is_fresher() -> None:
    view = get_stock("EL-LAP-01")
    assert view is not None and view["conflict"] is None
    assert view["resolved_source"] == "db" and view["resolved_stock"] == 12


def test_unknown_sku() -> None:
    assert get_stock("XX-NOP-99") is None


def test_bundle_keeps_both_values_visible() -> None:
    text = "Is EL-HP-02 in stock?"
    bundle = retrieve(Intent(summary=text, skus=["EL-HP-02"]), text)
    db_ev, csv_ev = bundle.get("db:products:EL-HP-02"), bundle.get("csv:inventory:EL-HP-02")
    assert db_ev and "DB stock 5" in db_ev.content
    assert csv_ev and "stock 0" in csv_ev.content
    assert [c.kind for c in bundle.conflicts].count("inventory") == 1


def test_no_inventory_conflict_for_other_skus() -> None:
    text = "Is the Trail Jacket in stock?"
    bundle = retrieve(Intent(summary=text, skus=["AP-JKT-03"]), text)
    assert not [c for c in bundle.conflicts if c.kind == "inventory"]
