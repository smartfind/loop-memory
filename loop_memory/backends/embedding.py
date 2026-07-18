"""Embedding backend abstractions.

The library is zero-dependency by default. This module ships a
``HashingEmbedder`` that produces deterministic bag-of-words style
vectors — good enough for demos and tests. Real users plug in OpenAI,
sentence-transformers, BGE, etc. via ``BaseEmbedder``.
"""

from __future__ import annotations

import hashlib
import math
import re

from ..memory.types import MemoryItem

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


class BaseEmbedder:
    """Subclass and implement ``embed`` and ``embed_query``."""

    dim: int = 0

    def embed(self, items: list[MemoryItem]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        raise NotImplementedError


class HashingEmbedder(BaseEmbedder):
    """Deterministic hashed bag-of-words embedder.

    Not semantically meaningful, but stable, dependency-free, and good
    for tests. Useful when no real embedding model is available.
    """

    def __init__(self, dim: int = 256, ngram_range: tuple = (1, 2)) -> None:
        self.dim = dim
        self.ngram_range = ngram_range

    def _vec(self, tokens: list[str]) -> list[float]:
        vec = [0.0] * self.dim
        for n in range(self.ngram_range[0], self.ngram_range[1] + 1):
            for i in range(len(tokens) - n + 1):
                gram = " ".join(tokens[i : i + n])
                h = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
                idx = h % self.dim
                sign = 1.0 if (h >> 8) & 1 else -1.0
                vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1e-12
        return [v / norm for v in vec]

    def embed(self, items: list[MemoryItem]) -> list[list[float]]:
        return [self._vec(_tokenize(it.text)) for it in items]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(_tokenize(text))

    def embed_text(self, text: str) -> list[float]:
        return self._vec(_tokenize(text))


class IdentityEmbedder(BaseEmbedder):
    """Pass-through embedder — store None; retrieval becomes score-only.

    Useful when you don't want vector search at all and prefer ranking
    by importance/recency alone.
    """

    dim = 0

    def embed(self, items: list[MemoryItem]) -> list[list[float]]:
        return [None]  # type: ignore[return-value]

    def embed_query(self, text: str) -> None:  # type: ignore[override]
        return None
