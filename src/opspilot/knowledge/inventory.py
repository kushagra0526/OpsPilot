"""Warehouse CSV feed merged with DB stock, with freshness-based conflict resolution."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from opspilot.agent.schemas import ConflictNote, Evidence
from opspilot.config import INVENTORY_CSV
from opspilot.knowledge.structured import get_product


def read_feed(path: Path = INVENTORY_CSV) -> dict[str, dict[str, Any]]:
    """Load the inventory feed keyed by SKU."""
    with path.open(encoding="utf-8", newline="") as fh:
        return {row["sku"]: {**row, "stock": int(row["stock"])} for row in csv.DictReader(fh)}


def _fmt(ts: str) -> str:
    return datetime.fromisoformat(ts).strftime("%Y-%m-%dT%H:%M")


def get_stock(sku: str) -> dict[str, Any] | None:
    """DB + CSV stock with timestamps, the resolved value and an optional ConflictNote."""
    product = get_product(sku)
    feed = read_feed().get(sku)
    if product is None and feed is None:
        return None
    view: dict[str, Any] = {
        "sku": sku,
        "name": product["name"] if product else None,
        "category": product["category"] if product else None,
        "db_stock": product["stock"] if product else None,
        "db_updated_at": product["stock_updated_at"] if product else None,
        "csv_stock": feed["stock"] if feed else None,
        "csv_snapshot_at": feed["snapshot_at"] if feed else None,
        "warehouse": feed["warehouse"] if feed else None,
        "conflict": None,
    }
    if product is None or feed is None:
        source = "db" if product else "csv"
        view.update(resolved_stock=view[f"{source}_stock"], resolved_source=source)
        return view
    csv_fresher = datetime.fromisoformat(feed["snapshot_at"]) > datetime.fromisoformat(
        product["stock_updated_at"]
    )
    source = "csv" if csv_fresher else "db"
    view.update(resolved_stock=view[f"{source}_stock"], resolved_source=source)
    if feed["stock"] != product["stock"]:
        fresh, stale = (
            (
                f"CSV feed (snapshot {_fmt(feed['snapshot_at'])})",
                f"DB ({_fmt(product['stock_updated_at'])})",
            )
            if csv_fresher
            else (
                f"DB ({_fmt(product['stock_updated_at'])})",
                f"CSV feed (snapshot {_fmt(feed['snapshot_at'])})",
            )
        )
        view["conflict"] = ConflictNote(
            kind="inventory",
            detail=(
                f"Stock for {sku} differs: DB says {product['stock']} (updated "
                f"{_fmt(product['stock_updated_at'])}), CSV feed says {feed['stock']} (snapshot "
                f"{_fmt(feed['snapshot_at'])})."
            ),
            resolution=f"Using {fresh} as it is fresher than {stale}. Resolved stock: {view['resolved_stock']}.",
            evidence_ids=[f"db:products:{sku}", f"csv:inventory:{sku}"],
        )
    return view


def stock_evidence(view: dict[str, Any]) -> list[Evidence]:
    """Evidence items for both stock sources (both stay visible even when they conflict)."""
    items: list[Evidence] = []
    sku = view["sku"]
    resolved = (
        f" Resolved stock (fresher source = {view['resolved_source']}): {view['resolved_stock']}."
    )
    if view["db_stock"] is not None:
        items.append(
            Evidence(
                id=f"db:products:{sku}",
                source_type="db",
                content=(
                    f"Product {sku} {view['name']} (category {view['category']}): DB stock "
                    f"{view['db_stock']} as of {view['db_updated_at']}." + resolved
                ),
                metadata={"table": "products", "updated_at": view["db_updated_at"]},
            )
        )
    if view["csv_stock"] is not None:
        items.append(
            Evidence(
                id=f"csv:inventory:{sku}",
                source_type="csv",
                content=(
                    f"Warehouse feed {view['warehouse']}: {sku} stock {view['csv_stock']} at snapshot "
                    f"{view['csv_snapshot_at']}." + resolved
                ),
                metadata={"snapshot_at": view["csv_snapshot_at"], "warehouse": view["warehouse"]},
            )
        )
    return items
