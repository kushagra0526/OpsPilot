"""SQLite connection helper, schema and small query helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opspilot.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    tier TEXT NOT NULL CHECK(tier IN ('standard','gold','platinum')),
    preferred_refund_method TEXT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products(
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('electronics','apparel','home','grocery')),
    price REAL NOT NULL,
    stock INTEGER NOT NULL,
    stock_updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders(
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL CHECK(status IN ('placed','shipped','delivered','cancelled','refunded','partially_refunded')),
    order_date TEXT NOT NULL,
    delivery_date TEXT NULL,
    total REAL NOT NULL,
    payment_method TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items(
    id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    sku TEXT NOT NULL REFERENCES products(sku),
    qty INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds(
    id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    amount REAL NOT NULL,
    method TEXT NOT NULL,
    reason TEXT,
    approved_by TEXT NULL,
    created_at TEXT NOT NULL,
    idempotency_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS coupons(
    code TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    percent_off INTEGER NOT NULL,
    amount_credit REAL NULL,
    reason TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets(
    id TEXT PRIMARY KEY,
    customer_id TEXT NULL,
    order_id TEXT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox(
    id INTEGER PRIMARY KEY,
    customer_id TEXT,
    to_email TEXT,
    subject TEXT,
    body TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    event TEXT NOT NULL CHECK(event IN ('proposed','approval_requested','approved','rejected','executed','failed','skipped','refused')),
    tool TEXT,
    args_json TEXT,
    rationale TEXT,
    risk_level TEXT,
    actor TEXT,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS long_term_memory(
    id INTEGER PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('customer','operator')),
    subject_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('preference','fact','interaction_summary')),
    content TEXT NOT NULL,
    source_session TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(subject_type, subject_id, kind, content)
);
CREATE TABLE IF NOT EXISTS ui_turns(
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_input TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ui_turns_session ON ui_turns(session_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
"""

TABLES = [
    "ui_turns",
    "audit_log",
    "long_term_memory",
    "outbox",
    "tickets",
    "coupons",
    "refunds",
    "order_items",
    "orders",
    "products",
    "customers",
]


def db_path() -> Path:
    """Path of the runtime SQLite database."""
    return get_settings().db_path


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a new connection (one per call/thread) with WAL, FKs and a busy timeout."""
    conn = sqlite3.connect(str(path or db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def transaction(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Connection context that commits on success, rolls back on error and always closes."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist."""
    conn.executescript(SCHEMA)


def drop_all(conn: sqlite3.Connection) -> None:
    """Drop every OpsPilot table (used by the idempotent seed)."""
    conn.execute("PRAGMA foreign_keys=OFF")
    for table in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA foreign_keys=ON")


def query(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
    """Run a SELECT and return rows as dicts."""
    conn = connect()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_one(sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
    """Run a SELECT and return the first row as a dict (or None)."""
    rows = query(sql, params)
    return rows[0] if rows else None


def table_counts() -> dict[str, int]:
    """Row count per table."""
    conn = connect()
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in reversed(TABLES)
        }
    finally:
        conn.close()
