"""Rebuild all runtime data (SQLite + Chroma). Idempotent: safe to run any number of times."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from opspilot.seeding import seed_all


def main() -> None:
    """Seed everything and print a summary table."""
    counts = seed_all()
    table = Table(title="OpsPilot seed summary")
    table.add_column("Store")
    table.add_column("Name")
    table.add_column("Rows", justify="right")
    for name, n in counts["tables"].items():
        table.add_row("SQLite", name, str(n))
    for name, n in counts["collections"].items():
        table.add_row("Chroma", name, str(n))
    Console().print(table)


if __name__ == "__main__":
    main()
