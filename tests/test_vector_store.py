"""Tests for the vector-store backends."""

from __future__ import annotations

import unittest

from loop_memory import (
    HashingEmbedder,
    InMemoryVectorStore,
    MemoryItem,
)


class InMemoryVectorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryVectorStore()
        self.embedder = HashingEmbedder(dim=64)

    def _item(self, text: str, vec_seed: str) -> MemoryItem:
        item = MemoryItem(text=text)
        item.embedding = self.embedder.embed_text(vec_seed)
        return item

    def test_add_requires_embedding(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add([MemoryItem(text="no embedding")])

    def test_search_returns_nearest(self) -> None:
        self.store.add([
            self._item("cat", "cat"),
            self._item("kitten", "kitten"),
            self._item("banana", "banana"),
        ])
        q = self.embedder.embed_text("kitty")
        top = self.store.search(q, top_k=2)
        texts = [it.text for it in top]
        self.assertIn("kitten", texts)
        self.assertIn("cat", texts)
        self.assertNotIn("banana", texts)

    def test_delete_removes_ids(self) -> None:
        a = self._item("cat", "cat")
        b = self._item("dog", "dog")
        self.store.add([a, b])
        self.assertEqual(self.store.count(), 2)
        removed = self.store.delete([a.id])
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.count(), 1)
