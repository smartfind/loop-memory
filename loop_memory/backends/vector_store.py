"""Pluggable vector-store abstraction.

By default the loop engine keeps everything in-process via
``HashingEmbedder``. For production workloads you can swap in any
backend that implements ``VectorStore``: ChromaDB, FAISS, LanceDB,
pgvector, etc.

The store contract is intentionally minimal — add / search / delete —
so wiring a new backend is a small subclass.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from ..memory.types import MemoryItem


class VectorStore(Protocol):
    def add(self, items: list[MemoryItem]) -> None: ...
    def search(self, query_embedding: list[float], top_k: int = 5) -> list[MemoryItem]: ...
    def delete(self, ids: Iterable[str]) -> int: ...
    def count(self) -> int: ...


@dataclass
class InMemoryVectorStore:
    """Reference implementation backed by a Python list.

    Useful for unit tests and tiny personal agents. Not suitable for
    corpora larger than a few thousand items.
    """

    _items: list[MemoryItem] = field(default_factory=list)

    def add(self, items: list[MemoryItem]) -> None:
        for it in items:
            if it.embedding is None:
                raise ValueError(f"MemoryItem {it.id!r} has no embedding")
        self._items.extend(items)

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[MemoryItem]:
        from ..memory.types import cosine_similarity

        scored = [
            (cosine_similarity(query_embedding, it.embedding or []), it)
            for it in self._items
            if it.embedding is not None
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored[:top_k]]

    def delete(self, ids: Iterable[str]) -> int:
        ids_set = set(ids)
        before = len(self._items)
        self._items = [it for it in self._items if it.id not in ids_set]
        return before - len(self._items)

    def count(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)


@dataclass
class ChromaVectorStore:
    """Optional ChromaDB backend. Imported lazily.

    Install with::

        pip install loop-memory[chroma]

    Example::

        store = ChromaVectorStore(collection="user_42")
        engine = LoopEngine(llm=..., embedder=my_embedder, vector_store=store)
    """

    collection: str = "loop_memory"
    persist_directory: str | None = None

    def __post_init__(self) -> None:
        try:
            import chromadb  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "chromadb is not installed; run `pip install loop-memory[chroma]`"
            ) from e
        client = (
            chromadb.PersistentClient(path=self.persist_directory)
            if self.persist_directory
            else chromadb.Client()
        )
        self._collection = client.get_or_create_collection(name=self.collection)

    def add(self, items: list[MemoryItem]) -> None:
        for it in items:
            if it.embedding is None:
                raise ValueError(f"MemoryItem {it.id!r} has no embedding")
            self._collection.add(
                ids=[it.id],
                embeddings=[it.embedding],
                documents=[it.text],
                metadatas=[{"kind": it.kind, "importance": it.importance}],
            )

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[MemoryItem]:
        res = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
        )
        out: list[MemoryItem] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        for i, _id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            out.append(
                MemoryItem(
                    id=_id,
                    text=docs[i] if i < len(docs) else "",
                    importance=float(meta.get("importance", 0.5)),
                    kind=str(meta.get("kind", "fact")),
                )
            )
        return out

    def delete(self, ids: Iterable[str]) -> int:
        ids_list = list(ids)
        if not ids_list:
            return 0
        self._collection.delete(ids=ids_list)
        return len(ids_list)

    def count(self) -> int:
        return int(self._collection.count())
