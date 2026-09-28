"""Seed data matches SPEC §4.3 exactly; Chroma has one chunk per policy section."""

from __future__ import annotations

import csv

import pytest

from opspilot import db
from opspilot.config import INVENTORY_CSV
from opspilot.knowledge import vector_store
from opspilot.seeding import seed_all


def _order(oid: str) -> dict:
    row = db.query_one("SELECT * FROM orders WHERE id=?", (oid,))
    assert row is not None, oid
    row["items"] = db.query("SELECT sku, qty, unit_price FROM order_items WHERE order_id=?", (oid,))
    return row


def test_demo_customers(fresh_db) -> None:
    expected = {
        "C-001": ("Aarav Mehta", "gold"),
        "C-007": ("Priya Nair", "platinum"),
        "C-012": ("Rohan Das", "standard"),
    }
    for cid, (name, tier) in expected.items():
        row = db.query_one("SELECT * FROM customers WHERE id=?", (cid,))
        assert row and row["name"] == name and row["tier"] == tier
    assert db.query_one("SELECT preferred_refund_method FROM customers WHERE id='C-007'") == {
        "preferred_refund_method": None
    }


@pytest.mark.parametrize(
    ("sku", "name", "category", "price"),
    [
        ("EL-LAP-01", "Nova 14 Laptop", "electronics", 1299.0),
        ("EL-HP-02", "Nova Buds Pro", "electronics", 149.0),
        ("AP-JKT-03", "Trail Jacket", "apparel", 89.0),
        ("GR-COF-04", "Single-Origin Coffee 1kg", "grocery", 24.0),
    ],
)
def test_demo_products(fresh_db, sku: str, name: str, category: str, price: float) -> None:
    row = db.query_one("SELECT * FROM products WHERE sku=?", (sku,))
    assert row and (row["name"], row["category"], row["price"]) == (name, category, price)


def test_conflict_product_db_values(fresh_db) -> None:
    row = db.query_one("SELECT stock, stock_updated_at FROM products WHERE sku='EL-HP-02'")
    assert row == {"stock": 5, "stock_updated_at": "2026-09-20T09:00:00"}


@pytest.mark.parametrize(
    ("oid", "cust", "status", "order_date", "delivery", "total", "skus"),
    [
        ("ORD-1001", "C-001", "delivered", "2026-09-17", "2026-09-22", 1299.0, ["EL-LAP-01"]),
        ("ORD-1002", "C-001", "delivered", "2026-08-05", "2026-08-10", 89.0, ["AP-JKT-03"]),
        ("ORD-1003", "C-012", "shipped", "2026-09-05", None, 79.0, ["AP-SNK-12"]),
        ("ORD-1004", "C-001", "delivered", "2026-09-10", None, 149.0, ["EL-HP-02"]),
        ("ORD-1005", "C-012", "delivered", "2026-09-20", "2026-09-24", 24.0, ["GR-COF-04"]),
        (
            "ORD-1006",
            "C-007",
            "delivered",
            "2026-09-15",
            "2026-09-20",
            238.0,
            ["AP-JKT-03", "EL-HP-02"],
        ),
        ("ORD-1007", "C-001", "delivered", "2026-09-14", "2026-09-18", 149.0, ["EL-HP-02"]),
        ("ORD-1008", "C-007", "placed", "2026-09-26", None, 149.0, ["EL-HP-02"]),
    ],
)
def test_demo_orders(fresh_db, oid, cust, status, order_date, delivery, total, skus) -> None:
    o = _order(oid)
    assert (o["customer_id"], o["status"], o["order_date"], o["delivery_date"], o["total"]) == (
        cust,
        status,
        order_date,
        delivery,
        total,
    )
    assert o["payment_method"] == "card"
    assert sorted(i["sku"] for i in o["items"]) == sorted(skus)
    assert all(i["qty"] == 1 for i in o["items"])


def test_background_volume(fresh_db) -> None:
    counts = db.table_counts()
    assert counts["customers"] == 20
    assert counts["products"] == 25
    assert counts["orders"] == 70
    cats = {r["category"] for r in db.query("SELECT DISTINCT category FROM products")}
    assert cats == {"electronics", "apparel", "home", "grocery"}


def test_inventory_csv_has_exactly_one_conflict(fresh_db) -> None:
    with INVENTORY_CSV.open(encoding="utf-8") as fh:
        rows = {r["sku"]: r for r in csv.DictReader(fh)}
    products = {p["sku"]: p for p in db.query("SELECT sku, stock FROM products")}
    assert set(rows) == set(products)
    diffs = [s for s, r in rows.items() if int(r["stock"]) != products[s]["stock"]]
    assert diffs == ["EL-HP-02"]
    assert rows["EL-HP-02"]["stock"] == "0"
    assert rows["EL-HP-02"]["snapshot_at"] == "2026-09-26T18:00:00"


def test_chroma_one_chunk_per_section() -> None:
    parsed = vector_store.load_all_policy_chunks()
    stored = {c["id"]: c["metadata"] for c in vector_store.get_policy_chunks()}
    assert len(stored) == len(parsed) == 38
    for chunk in parsed:
        meta = stored[chunk.chunk_id]
        assert chunk.chunk_id == f"{meta['doc_id']}§{meta['section']}"
        for key in (
            "doc_id",
            "title",
            "version",
            "effective_date",
            "supersedes",
            "section",
            "category",
        ):
            assert key in meta
    v2 = stored["return_policy_v2§2"]
    assert v2["version"] == 2 and v2["effective_date"] == "2026-03-01"
    assert v2["supersedes"] == "return_policy_v1"


def test_seed_is_idempotent(tmp_path, monkeypatch) -> None:
    # Rebuild a separate runtime: re-ingesting the shared session store mid-run would
    # delete the Chroma collections every later retrieval test depends on.
    monkeypatch.setenv("OPSPILOT_RUNTIME_DIR", str(tmp_path))
    first = seed_all()
    second = seed_all()
    assert first == second
    assert vector_store.collection_counts() == {"policies": 38, "faq": 17}
