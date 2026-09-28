"""Deterministic seed data: background records plus the hand-crafted demo records (SPEC §4.3)."""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from typing import Any

from opspilot import db
from opspilot.config import get_settings
from opspilot.knowledge import vector_store

# (sku, name, category, price, stock)
CATALOG: list[tuple[str, str, str, float, int]] = [
    ("EL-LAP-01", "Nova 14 Laptop", "electronics", 1299.0, 12),
    ("EL-HP-02", "Nova Buds Pro", "electronics", 149.0, 5),
    ("EL-SPK-05", "Nova Mini Speaker", "electronics", 59.0, 40),
    ("EL-CHG-06", "Nova 65W Charger", "electronics", 35.0, 80),
    ("EL-MON-07", "Nova 27 Monitor", "electronics", 329.0, 9),
    ("EL-KBD-08", "Nova Mechanical Keyboard", "electronics", 99.0, 25),
    ("EL-WCH-09", "Nova Fit Watch", "electronics", 199.0, 18),
    ("AP-JKT-03", "Trail Jacket", "apparel", 89.0, 30),
    ("AP-TSH-10", "Everyday Cotton Tee", "apparel", 19.0, 150),
    ("AP-JNS-11", "Slim Fit Jeans", "apparel", 49.0, 60),
    ("AP-SNK-12", "Runner Sneakers", "apparel", 79.0, 35),
    ("AP-HOD-13", "Fleece Hoodie", "apparel", 45.0, 50),
    ("AP-CAP-14", "Canvas Cap", "apparel", 15.0, 90),
    ("HM-LMP-15", "Arc Desk Lamp", "home", 39.0, 45),
    ("HM-MUG-16", "Stoneware Mug Set", "home", 29.0, 70),
    ("HM-PIL-17", "Memory Foam Pillow", "home", 35.0, 40),
    ("HM-BLK-18", "Wool Throw Blanket", "home", 59.0, 20),
    ("HM-PAN-19", "Cast Iron Pan", "home", 45.0, 25),
    ("GR-COF-04", "Single-Origin Coffee 1kg", "grocery", 24.0, 100),
    ("GR-TEA-20", "Assam Tea 500g", "grocery", 12.0, 120),
    ("GR-OIL-21", "Cold-Pressed Olive Oil 1L", "grocery", 18.0, 60),
    ("GR-NUT-22", "Mixed Nuts 750g", "grocery", 16.0, 80),
    ("GR-HON-23", "Wild Honey 500g", "grocery", 11.0, 70),
    ("GR-CHO-24", "Dark Chocolate Bar Pack", "grocery", 9.0, 150),
    ("GR-OAT-25", "Rolled Oats 2kg", "grocery", 8.0, 90),
]
CONFLICT_SKU = "EL-HP-02"
CONFLICT_DB_UPDATED = "2026-09-20T09:00:00"
DB_STOCK_UPDATED = "2026-09-25T08:00:00"

DEMO_CUSTOMERS = [
    ("C-001", "Aarav Mehta", "aarav.mehta@example.com", "gold", None),
    ("C-007", "Priya Nair", "priya.nair@example.com", "platinum", None),
    ("C-012", "Rohan Das", "rohan.das@example.com", "standard", None),
]

FIRST = [
    "Ananya",
    "Vikram",
    "Meera",
    "Kabir",
    "Isha",
    "Arjun",
    "Diya",
    "Neel",
    "Sara",
    "Dev",
    "Tara",
    "Rahul",
    "Nisha",
    "Omar",
    "Leela",
    "Karan",
    "Zoya",
    "Aditya",
    "Maya",
    "Ravi",
]
LAST = [
    "Sharma",
    "Iyer",
    "Kapoor",
    "Reddy",
    "Singh",
    "Gupta",
    "Menon",
    "Khan",
    "Bose",
    "Patel",
    "Joshi",
    "Rao",
    "Verma",
    "Pillai",
    "Chopra",
]


def _d(ref: date, days_ago: int) -> str:
    return (ref - timedelta(days=days_ago)).isoformat()


def demo_orders(ref: date) -> list[dict[str, Any]]:
    """The fixed demo orders from SPEC §4.3 (dates relative to the reference date)."""

    def order(
        oid: str,
        cust: str,
        status: str,
        ordered: int,
        delivered: int | None,
        items: list[tuple[str, int, float]],
    ) -> dict[str, Any]:
        return {
            "id": oid,
            "customer_id": cust,
            "status": status,
            "order_date": _d(ref, ordered),
            "delivery_date": _d(ref, delivered) if delivered is not None else None,
            "total": round(sum(q * p for _, q, p in items), 2),
            "payment_method": "card",
            "items": items,
        }

    return [
        order("ORD-1001", "C-001", "delivered", 10, 5, [("EL-LAP-01", 1, 1299.0)]),
        order("ORD-1002", "C-001", "delivered", 53, 48, [("AP-JKT-03", 1, 89.0)]),
        order("ORD-1003", "C-012", "shipped", 22, None, [("AP-SNK-12", 1, 79.0)]),
        order("ORD-1004", "C-001", "delivered", 17, None, [("EL-HP-02", 1, 149.0)]),
        order("ORD-1005", "C-012", "delivered", 7, 3, [("GR-COF-04", 1, 24.0)]),
        order(
            "ORD-1006",
            "C-007",
            "delivered",
            12,
            7,
            [("AP-JKT-03", 1, 89.0), ("EL-HP-02", 1, 149.0)],
        ),
        order("ORD-1007", "C-001", "delivered", 13, 9, [("EL-HP-02", 1, 149.0)]),
        order("ORD-1008", "C-007", "placed", 1, None, [("EL-HP-02", 1, 149.0)]),
        # Fixtures for the held-out eval set (evals/holdout.yaml), on non-demo customers:
        # ORD-3001 monitor bought on promotion at exactly $200 (auto-refund boundary),
        # ORD-3002 platinum customer's apparel/home order 40 days after delivery.
        order("ORD-3001", "C-003", "delivered", 10, 6, [("EL-MON-07", 1, 200.0)]),
        order(
            "ORD-3002",
            "C-004",
            "delivered",
            45,
            40,
            [("AP-HOD-13", 1, 45.0), ("HM-LMP-15", 1, 39.0)],
        ),
    ]


def _insert_order(conn: sqlite3.Connection, o: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO orders(id, customer_id, status, order_date, delivery_date, total, payment_method)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            o["id"],
            o["customer_id"],
            o["status"],
            o["order_date"],
            o["delivery_date"],
            o["total"],
            o["payment_method"],
        ),
    )
    for sku, qty, price in o["items"]:
        conn.execute(
            "INSERT INTO order_items(order_id, sku, qty, unit_price) VALUES (?,?,?,?)",
            (o["id"], sku, qty, price),
        )


def _seed_customers(conn: sqlite3.Connection, rng: random.Random, ref: date) -> list[str]:
    demo_ids = {c[0] for c in DEMO_CUSTOMERS}
    fixed = {c[0]: c for c in DEMO_CUSTOMERS}
    background: list[str] = []
    for n in range(1, 21):
        cid = f"C-{n:03d}"
        created = _d(ref, rng.randint(200, 900))
        if cid in demo_ids:
            _, name, email, tier, pref = fixed[cid]
        else:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            email = f"{name.lower().replace(' ', '.')}.{n}@example.com"
            tier = rng.choice(["standard", "standard", "gold", "platinum"])
            pref = None
            background.append(cid)
        conn.execute(
            "INSERT INTO customers(id, name, email, tier, preferred_refund_method, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (cid, name, email, tier, pref, created),
        )
    return background


def _seed_background_orders(
    conn: sqlite3.Connection, rng: random.Random, ref: date, customers: list[str]
) -> None:
    for n in range(1, 61):
        age = rng.randint(1, 120)
        if age <= 6:
            status, delivered = rng.choice(["placed", "shipped"]), None
        else:
            status = rng.choice(["delivered"] * 8 + ["cancelled"])
            delivered = age - rng.randint(2, 6) if status == "delivered" else None
        items = [
            (sku, rng.randint(1, 2), price)
            for sku, _, _, price, _ in rng.sample(CATALOG, rng.randint(1, 3))
        ]
        _insert_order(
            conn,
            {
                "id": f"ORD-{2000 + n}",
                "customer_id": rng.choice(customers),
                "status": status,
                "order_date": _d(ref, age),
                "delivery_date": _d(ref, delivered) if delivered is not None else None,
                "total": round(sum(q * p for _, q, p in items), 2),
                "payment_method": rng.choice(["card", "upi", "wallet"]),
                "items": items,
            },
        )


def seed_database() -> None:
    """Drop and rebuild the SQLite database deterministically."""
    ref = get_settings().reference_date
    rng = random.Random(42)
    with db.transaction() as conn:
        db.drop_all(conn)
        db.init_schema(conn)
        background = _seed_customers(conn, rng, ref)
        for sku, name, category, price, stock in CATALOG:
            updated = CONFLICT_DB_UPDATED if sku == CONFLICT_SKU else DB_STOCK_UPDATED
            conn.execute(
                "INSERT INTO products(sku, name, category, price, stock, stock_updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (sku, name, category, price, stock, updated),
            )
        for o in demo_orders(ref):
            _insert_order(conn, o)
        _seed_background_orders(conn, rng, ref, background)


def seed_all() -> dict[str, dict[str, int]]:
    """Rebuild SQLite and Chroma. Returns counts for the summary table."""
    seed_database()
    chunks = vector_store.ingest_all()
    return {"tables": db.table_counts(), "collections": chunks}
