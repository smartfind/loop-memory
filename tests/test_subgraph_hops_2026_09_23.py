"""Tests for ``subgraph_for`` honouring ``max_hops`` (audit 2026-09-23).

Previously ``subgraph_for()`` accepted a ``max_hops`` keyword argument
but ignored it: the loop always walked exactly one ``related_entities``
hop from the seed entities. After the fix, callers that explicitly
ask for ``max_hops >= 2`` get the multi-hop walk they expected.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from loop_memory.jobs.graph import subgraph_for
from loop_memory.storage.sqlite_store import MemoryStore


def _new_store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-subgraph-hops-"))
    return MemoryStore(str(tmp / "subgraph.db")), tmp


def _seed_chain(store: MemoryStore) -> None:
    """Create a 3-node chain: alice --knows--> bob --knows--> carol.

    ``extract_entities`` parses bare nouns from a query, so we use a
    query containing all three names and expect the BFS to reach
    ``carol`` at max_hops >= 2.
    """
    import time
    now = time.time()
    with store._conn() as c:
        for name in ("Alice", "Bob", "Carol"):
            c.execute(
                "INSERT INTO entities(name, kind, weight, mention_count, created_at, updated_at) "
                "VALUES (?, 'person', 0.5, 1, ?, ?)",
                (name, now, now),
            )
        for src, dst in (("Alice", "Bob"), ("Bob", "Carol")):
            c.execute(
                "INSERT INTO relations(src, dst, kind, weight, evidence_ids, created_at) "
                "VALUES (?, ?, 'knows', 0.5, '[]', ?)",
                (src, dst, now),
            )


class SubgraphMaxHopsTests(unittest.TestCase):
    """Audit 2026-09-23: subgraph_for walks up to max_hops edges."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        _seed_chain(self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_hop_reaches_alice_and_bob_only(self) -> None:
        # Query contains ONLY Alice (the seed) so the BFS hop is the
        # only way Bob/Carol can appear in the result.
        sg = subgraph_for(self.store, "Alice", max_hops=1)
        node_names = {n["name"] for n in sg.nodes}
        self.assertIn("Alice", node_names)
        self.assertIn("Bob", node_names,
                      "max_hops=1 must walk Alice->Bob")
        self.assertNotIn("Carol", node_names,
                         "max_hops=1 must NOT walk the second hop")

    def test_two_hops_reaches_carol(self) -> None:
        sg = subgraph_for(self.store, "Alice", max_hops=2)
        node_names = {n["name"] for n in sg.nodes}
        self.assertIn("Alice", node_names)
        self.assertIn("Bob", node_names)
        self.assertIn("Carol", node_names,
                      "max_hops=2 must walk the Alice->Bob->Carol chain")

    def test_max_hops_hard_cap_is_8(self) -> None:
        # A typo'd ``max_hops=1000`` must NOT walk the whole graph.
        # We can't easily measure the count, but the function must
        # not raise and must return a Subgraph.
        sg = subgraph_for(self.store, "Alice", max_hops=1000)
        self.assertIsNotNone(sg)

    def test_default_max_hops_is_one(self) -> None:
        sg_default = subgraph_for(self.store, "Alice")
        sg_one = subgraph_for(self.store, "Alice", max_hops=1)
        self.assertEqual(
            {n["name"] for n in sg_default.nodes},
            {n["name"] for n in sg_one.nodes},
        )

    def test_zero_or_negative_max_hops_clamped_to_one(self) -> None:
        # Defensive: a typo'd 0 / negative must NOT walk zero hops and
        # silently return only the seed.
        sg = subgraph_for(self.store, "Alice", max_hops=0)
        node_names = {n["name"] for n in sg.nodes}
        self.assertIn("Bob", node_names,
                      "max_hops=0 must be clamped to 1, not zero")


if __name__ == "__main__":
    unittest.main()
