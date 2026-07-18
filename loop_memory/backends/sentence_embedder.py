"""Sentence-transformers backed embedder (optional dependency)."""

from __future__ import annotations

from ..memory.types import MemoryItem
from .embedding import BaseEmbedder


class SentenceTransformerEmbedder(BaseEmbedder):
    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers is not installed; "
                "run `pip install loop-memory[sentence]`"
            ) from e
        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, items: list[MemoryItem]) -> list[list[float]]:
        if not items:
            return []
        return [list(v) for v in self._model.encode([it.text for it in items])]

    def embed_query(self, text: str) -> list[float]:
        return list(self._model.encode([text])[0])

    def embed_text(self, text: str) -> list[float]:
        return self.embed_query(text)
