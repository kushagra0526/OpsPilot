"""FAQ search (semantic, over the `faq` Chroma collection)."""

from __future__ import annotations

from typing import Any

from opspilot.agent.schemas import Evidence
from opspilot.config import FAQ_TOP_K
from opspilot.knowledge import vector_store


def search(texts: list[str], k: int = FAQ_TOP_K) -> list[dict[str, Any]]:
    """Top-k FAQ hits for the query texts."""
    return vector_store.search_faq(texts, k)


def to_evidence(hit: dict[str, Any]) -> Evidence:
    """Convert an FAQ hit into evidence."""
    return Evidence(
        id=f"faq:{hit['id']}",
        source_type="faq",
        content=hit["document"],
        metadata={"similarity": hit["similarity"], "tags": hit["metadata"].get("tags", "")},
    )
