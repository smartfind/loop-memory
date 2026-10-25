"""CLI + HTTP + SDK coverage for the 0.4.16 dream / latest-only work.

Audit 2026-10-25 (loop-memory 0.4.16). Pins the public surfaces:

* ``loop-memory dream [--apply] [--json]``
* ``loop-memory recall --latest-only``
* ``GET /api/recall?latest_only=1``
* ``GET /api/v1/recall?latest_only=1``
* ``POST /api/v1/memories`` accepting ``subject_key``
* ``POST /api/v1/dream``
* ``MemoryClient.remember(subject_key=...)`` / ``recall(latest_only=...)``
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from fastapi.testclient import TestClient

from loop_memory.cli import commands
from loop_memory.cli.commands import write as write_cmd  # noqa: F401
from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


class DreamCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "cli.db")
        self.store = MemoryStore(self.db_path)
        self.old = time.time() - 86400 * 200

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.object(write_cmd, "default_db_path",
                               return_value=self.db_path):
            with redirect_stdout(buf):
                code = write_cmd.run_dream(args)
        return code, buf.getvalue()

    def test_help_lists_the_command(self) -> None:
        from loop_memory.cli.main import COMMAND_HELP, COMMANDS
        self.assertIn("dream", COMMANDS)
        self.assertIn("--apply", COMMAND_HELP["dream"])

    def test_dry_run_is_the_default(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        code, out = self._run([])
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertIsNone(self.store.get_memory(m.id).invalid_at)

    def test_apply_writes_the_expiry(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        code, out = self._run(["--apply"])
        self.assertEqual(code, 0)
        self.assertIn("applied", out)
        self.assertIsNotNone(self.store.get_memory(m.id).invalid_at)

    def test_json_output_is_parseable(self) -> None:
        code, out = self._run(["--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("expired", data)
        self.assertIn("actions", data)

    def test_unknown_flag_is_rejected(self) -> None:
        code, _ = self._run(["--nope"])
        self.assertEqual(code, 2)

    def test_non_numeric_value_is_rejected(self) -> None:
        code, _ = self._run(["--ttl-days", "abc"])
        self.assertEqual(code, 2)

    def test_ttl_days_flag_is_accepted(self) -> None:
        code, _ = self._run(["--ttl-days", "1"])
        self.assertEqual(code, 0)


class RecallLatestOnlyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "cli2.db")
        self.store = MemoryStore(self.db_path)
        self.now = time.time()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.object(commands.read, "default_db_path",
                               return_value=self.db_path):
            with redirect_stdout(buf):
                code = commands.read.run_recall(args)
        return code, buf.getvalue()

    def test_latest_only_flag_is_accepted(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        code, out = self._run(["recall", "deploy pipeline", "--latest-only"])
        self.assertEqual(code, 0)
        self.assertIn("gitlab ci", out)
        self.assertNotIn("github actions", out)

    def test_without_the_flag_both_rows_are_shown(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        code, out = self._run(["recall", "deploy pipeline"])
        self.assertEqual(code, 0)
        self.assertIn("gitlab ci", out)
        self.assertIn("github actions", out)


class DreamHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "http.db"))
        self.client = TestClient(create_app(store=self.store))
        self.now = time.time()
        self.old = self.now - 86400 * 200

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dream_route_dry_run(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        r = self.client.post("/api/v1/dream", json={"apply": False})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["expired"], 1)
        self.assertFalse(body["applied"])

    def test_dream_route_apply(self) -> None:
        m = self.store.upsert_memory(
            kind="fact", text="the old build server is decommissioned",
            importance=0.1, created_at=self.old, updated_at=self.old,
        )
        r = self.client.post("/api/v1/dream", json={"apply": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["applied"])
        self.assertIsNotNone(self.store.get_memory(m.id).invalid_at)

    def test_dream_route_rejects_bad_parameters(self) -> None:
        r = self.client.post("/api/v1/dream", json={"ttl_days": "soon"})
        self.assertEqual(r.status_code, 400)

    def test_dream_route_accepts_an_empty_body(self) -> None:
        r = self.client.post("/api/v1/dream", json={})
        self.assertEqual(r.status_code, 200)

    def test_recall_route_latest_only(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        r = self.client.get("/api/recall", params={
            "query": "deploy pipeline", "limit": 10, "latest_only": 1,
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["latest_only"])
        self.assertEqual(len(body["memories"]), 1)

    def test_recall_route_without_latest_only(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        r = self.client.get("/api/recall", params={
            "query": "deploy pipeline", "limit": 10,
        })
        body = r.json()
        self.assertFalse(body["latest_only"])
        self.assertEqual(len(body["memories"]), 2)

    def test_v1_recall_route_latest_only(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        r = self.client.get("/api/v1/recall", params={
            "q": "deploy pipeline", "limit": 10, "latest_only": 1,
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["latest_only"])
        self.assertEqual(len(body["memories"]), 1)

    def test_v1_memories_accepts_subject_key(self) -> None:
        r = self.client.post("/api/v1/memories", json={
            "text": "User prefers tabs",
            "subject_key": "editor.preference",
            "agent_id": "codex", "user_id": "u1",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["subject_key"], "editor.preference")

    def test_v1_memories_supersedes_the_same_slot(self) -> None:
        self.client.post("/api/v1/memories", json={
            "text": "User prefers tabs",
            "subject_key": "editor.preference",
            "agent_id": "codex", "user_id": "u1",
        })
        r = self.client.post("/api/v1/memories", json={
            "text": "User prefers spaces",
            "subject_key": "editor.preference",
            "agent_id": "codex", "user_id": "u1",
        })
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["superseded_by"])


class DreamSdkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "sdk.db"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remember_accepts_subject_key(self) -> None:
        from loop_memory.sdk import MemoryClient
        client = MemoryClient.memory(self.store, agent_id="codex", user_id="u1")
        m = client.remember(
            "User prefers tabs", subject_key="editor.preference",
        )
        self.assertEqual(m.subject_key, "editor.preference")

    def test_recall_latest_only_collapses_the_slot(self) -> None:
        from loop_memory.sdk import MemoryClient
        client = MemoryClient.memory(self.store, agent_id="codex", user_id="u1")
        client.remember("User prefers tabs", subject_key="editor.preference")
        client.remember("User prefers spaces", subject_key="editor.preference")
        result = client.recall("User prefers", latest_only=True)
        self.assertEqual(len(result.memories), 1)
        self.assertIn("spaces", result.memories[0].text)

    def test_recall_without_latest_only_keeps_the_legacy_contract(self) -> None:
        from loop_memory.sdk import MemoryClient
        client = MemoryClient.memory(self.store, agent_id="codex", user_id="u1")
        client.remember("User prefers tabs", subject_key="editor.preference")
        client.remember("User prefers spaces", subject_key="editor.preference")
        result = client.recall("User prefers")
        self.assertEqual(len(result.memories), 1)


if __name__ == "__main__":
    unittest.main()
