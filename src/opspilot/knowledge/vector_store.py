"""Chroma ingestion and semantic search for policies and FAQ entries."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb
import yaml
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from opspilot.config import FAQ_PATH, POLICY_DIR, get_settings

POLICY_COLLECTION = "policies"
FAQ_COLLECTION = "faq"

_clients: dict[str, Any] = {}
_embedder: DefaultEmbeddingFunction | None = None
_lock = threading.Lock()  # client/embedder creation is not thread-safe (parallel retrieval)


@dataclass
class PolicyChunk:
    """One numbered section of a policy document."""

    chunk_id: str
    text: str
    metadata: dict[str, Any]


def _get_embedder() -> DefaultEmbeddingFunction:
    global _embedder
    with _lock:
        if _embedder is None:
            _embedder = DefaultEmbeddingFunction()
        return _embedder


def get_client(path: Path | None = None) -> Any:
    """Cached persistent Chroma client for the runtime directory."""
    target = str(path or get_settings().chroma_dir)
    with _lock:
        if target not in _clients:
            _clients[target] = chromadb.PersistentClient(path=target)
        return _clients[target]


def _collection(name: str) -> Any:
    return get_client().get_or_create_collection(
        name, embedding_function=_get_embedder(), configuration={"hnsw": {"space": "cosine"}}
    )


def parse_policy(path: Path) -> list[PolicyChunk]:
    """Split a policy markdown file into one chunk per numbered `##` section."""
    raw = path.read_text(encoding="utf-8")
    _, front, body = raw.split("---", 2)
    meta = yaml.safe_load(front)
    doc_meta = {
        "doc_id": str(meta["doc_id"]),
        "title": str(meta["title"]),
        "version": int(meta["version"]),
        "effective_date": str(meta["effective_date"]),
        "supersedes": str(meta.get("supersedes") or ""),
        "category": str(meta["category"]),
    }
    chunks: list[PolicyChunk] = []
    for block in re.split(r"^## ", body, flags=re.MULTILINE)[1:]:
        heading, _, text = block.partition("\n")
        match = re.match(r"(\d+)\.\s*(.+)", heading.strip())
        if not match:
            continue
        section, section_title = match.group(1), match.group(2).strip()
        metadata = {**doc_meta, "section": section, "section_title": section_title}
        content = (
            f"{doc_meta['title']} v{doc_meta['version']} §{section} {section_title}: "
            f"{' '.join(text.split())}"
        )
        chunks.append(PolicyChunk(f"{doc_meta['doc_id']}§{section}", content, metadata))
    return chunks


def load_all_policy_chunks(policy_dir: Path = POLICY_DIR) -> list[PolicyChunk]:
    """Parse every policy file in the knowledge directory."""
    chunks: list[PolicyChunk] = []
    for path in sorted(policy_dir.glob("*.md")):
        chunks.extend(parse_policy(path))
    return chunks


def load_faq(path: Path = FAQ_PATH) -> list[dict[str, Any]]:
    """Load FAQ entries from JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def ingest_all() -> dict[str, int]:
    """Drop and rebuild both Chroma collections. Returns chunk counts per collection."""
    client = get_client()
    for name in (POLICY_COLLECTION, FAQ_COLLECTION):
        if name in [c.name for c in client.list_collections()]:
            client.delete_collection(name)
    chunks = load_all_policy_chunks()
    _collection(POLICY_COLLECTION).add(
        ids=[c.chunk_id for c in chunks],
        documents=[c.text for c in chunks],
        metadatas=[c.metadata for c in chunks],
    )
    faq = load_faq()
    _collection(FAQ_COLLECTION).add(
        ids=[f["id"] for f in faq],
        documents=[f"Q: {f['question']} A: {f['answer']}" for f in faq],
        metadatas=[
            {"faq_id": f["id"], "question": f["question"], "tags": ", ".join(f["tags"])}
            for f in faq
        ],
    )
    counts = {POLICY_COLLECTION: len(chunks), FAQ_COLLECTION: len(faq)}
    for name, expected in counts.items():
        _wait_until_queryable(name, expected)
    return counts


def _wait_until_queryable(name: str, expected: int, timeout: float = 30.0) -> None:
    """Block until a freshly rebuilt collection answers queries with all its items.

    Chroma builds the vector index in the background after `add`; a query that arrives
    first can fail ("Nothing found on disk") or miss items. Seeding waits so no caller
    ever sees a half-built store.
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            col = _collection(name)
            if col.count() == expected and col.query(query_texts=["probe"], n_results=1)["ids"][0]:
                return
        except Exception as exc:  # noqa: BLE001 - any Chroma error means "not ready yet"
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"Chroma collection {name!r} not queryable after rebuild: {last}")


def _query(
    name: str, texts: list[str], k: int, where: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    col = _collection(name)
    k = min(k, col.count())
    if k == 0 or not texts:
        return []
    res = col.query(query_texts=texts, n_results=k, where=where)
    best: dict[str, dict[str, Any]] = {}
    for q in range(len(texts)):
        for i, cid in enumerate(res["ids"][q]):
            sim = 1.0 - float(res["distances"][q][i])
            if cid not in best or sim > best[cid]["similarity"]:
                best[cid] = {
                    "id": cid,
                    "document": res["documents"][q][i],
                    "metadata": dict(res["metadatas"][q][i]),
                    "similarity": round(sim, 4),
                }
    return sorted(best.values(), key=lambda h: h["similarity"], reverse=True)


def search_policies(texts: list[str], k: int, doc_id: str | None = None) -> list[dict[str, Any]]:
    """Semantic search over policy chunks (optionally one doc); merges query texts by best score."""
    where = {"doc_id": doc_id} if doc_id else None
    return _query(POLICY_COLLECTION, [t for t in texts if t.strip()], k, where)


def search_faq(texts: list[str], k: int) -> list[dict[str, Any]]:
    """Semantic search over FAQ entries."""
    return _query(FAQ_COLLECTION, [t for t in texts if t.strip()], k)


def get_policy_chunks(doc_id: str | None = None) -> list[dict[str, Any]]:
    """All stored policy chunks (optionally for one document) with metadata."""
    col = _collection(POLICY_COLLECTION)
    kwargs: dict[str, Any] = {"include": ["documents", "metadatas"]}
    if doc_id:
        kwargs["where"] = {"doc_id": doc_id}
    res = col.get(**kwargs)
    return [
        {"id": cid, "document": doc, "metadata": dict(meta), "similarity": None}
        for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"], strict=True)
    ]


def collection_counts() -> dict[str, int]:
    """Number of items per Chroma collection."""
    return {name: _collection(name).count() for name in (POLICY_COLLECTION, FAQ_COLLECTION)}
