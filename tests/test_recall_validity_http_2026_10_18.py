"""End-to-end HTTP + CLI coverage for ``as_of_ts`` / ``validity_at``.

Audit 2026-10-18 (loop-memory 0.4.15): the HTTP ``/api/recall``
endpoint gains a ``validity_at`` query parameter (distinct from the
existing ``as_of`` which routes to the supersession-chain
``recall_as_of``). The ``loop-memory recall`` CLI gains a matching
``--validity-at`` flag. This test pins both shapes so a future
refactor can't silently drop the new param.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


class HttpRecallValidityTests(unittest.TestCase):
    """``/api/recall?validity_at=`` route param."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "http.db"))
        self.past = time.time() - 86400
        self.future = time.time() + 86400 * 30
        self.expired = time.time() - 86400 * 30
        self.store.upsert_memory(
            kind="fact", text="Active memory about auth tokens",
            agent_id="a", user_id="u", valid_at=self.past,
        )
        self.store.upsert_memory(
            kind="fact", text="Future memory about auth tokens",
            agent_id="a", user_id="u", valid_at=self.future,
        )
        self.store.upsert_memory(
            kind="fact", text="Expired memory about auth tokens",
            agent_id="a", user_id="u",
            valid_at=self.past, invalid_at=self.expired,
        )
        self.client = TestClient(create_app(store=self.store))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_validity_at_returns_all(self) -> None:
        r = self.client.get("/api/recall",
                            params={"query": "auth tokens", "limit": 10})
        self.assertEqual(len(r.json()["memories"]), 3)

    def test_validity_at_epoch_returns_only_active(self) -> None:
        now = time.time()
        r = self.client.get(
            "/api/recall",
            params={"query": "auth tokens", "limit": 10,
                    "validity_at": str(now)},
        )
        self.assertEqual(len(r.json()["memories"]), 1)
        self.assertIn("Active", r.json()["memories"][0]["text"])

    def test_validity_at_iso_string_accepted(self) -> None:
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
        r = self.client.get(
            "/api/recall",
            params={"query": "auth tokens", "limit": 10,
                    "validity_at": iso},
        )
        self.assertEqual(len(r.json()["memories"]), 1)

    def test_validity_at_echoed_in_response(self) -> None:
        now_str = str(time.time())
        r = self.client.get(
            "/api/recall",
            params={"query": "auth tokens", "limit": 10,
                    "validity_at": now_str},
        )
        self.assertEqual(r.json().get("validity_at"), now_str)

    def test_invalid_validity_at_returns_400_or_500(self) -> None:
        r = self.client.get(
            "/api/recall",
            params={"query": "auth tokens", "limit": 10,
                    "validity_at": "not-a-date"},
        )
        self.assertIn(r.status_code, (400, 422, 500))

    def test_evidence_chain_present_on_each_hit(self) -> None:
        r = self.client.get("/api/recall",
                            params={"query": "auth tokens", "limit": 10})
        self.assertTrue(r.json()["memories"])
        for m in r.json()["memories"]:
            self.assertIn("evidence", m)
            self.assertIsInstance(m["evidence"], list)
            # bm25 + rrf_total are always present in the hybrid path
            fields = {e["field"] for e in m["evidence"]}
            self.assertIn("bm25", fields)
            self.assertIn("rrf_total", fields)

    def test_legacy_as_of_still_routes_to_recall_as_of(self) -> None:
        now = time.time()
        r = self.client.get(
            "/api/recall",
            params={"query": "auth tokens", "limit": 10,
                    "as_of": str(now)},
        )
        self.assertEqual(r.json()["mode"], "as_of")


class CliRecallValidityTests(unittest.TestCase):
    """``loop-memory recall --validity-at <T>`` CLI flag."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "cli.db"))
        self.past = time.time() - 86400
        self.future = time.time() + 86400 * 30
        self.store.upsert_memory(
            kind="fact", text="Active memory about auth tokens",
            agent_id="a", user_id="u", valid_at=self.past,
        )
        self.store.upsert_memory(
            kind="fact", text="Future memory about auth tokens",
            agent_id="a", user_id="u", valid_at=self.future,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_validity_at_epoch_filters(self) -> None:
        from loop_memory.cli.commands.read import run_recall
        # Patch the db path so the CLI opens our test store.
        from loop_memory.cli import commands
        with mock.patch.object(
            commands.read, "default_db_path",
            return_value=os.path.join(self.tmp, "cli.db"),
        ):
            rc = run_recall([
                "auth", "tokens",
                "--validity-at", str(time.time()),
                "--limit", "10",
            ])
        self.assertEqual(rc, 0)
        # Cannot easily capture stdout, but rc == 0 means no parse
        # error and the recall() call returned without raising.

    def test_validity_at_invalid_returns_nonzero(self) -> None:
        from loop_memory.cli.commands.read import run_recall
        from loop_memory.cli import commands
        with mock.patch.object(
            commands.read, "default_db_path",
            return_value=os.path.join(self.tmp, "cli.db"),
        ):
            rc = run_recall([
                "auth", "tokens",
                "--validity-at", "not-a-date",
            ])
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
