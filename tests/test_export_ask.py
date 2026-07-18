"""API + CLI tests for /api/wiki/export, /api/wiki/ask and the export/ask CLI."""
import os
import shutil
import tempfile
import unittest

from loop_memory.storage.sqlite_store import MemoryStore


def _seed_wiki(store: MemoryStore) -> int:
    sid = store.upsert_session(
        source="t", external_id="seed",
        title="seed", started_at=1700000000.0,
    ).id
    for i in range(3):
        store.upsert_memory(
            kind="fact", text=f"src-{i}", importance=0.7,
            session_id=sid, source=f"t/{i}", created_at=1700000000 - i * 3600,
        )
    page = store.upsert_wiki_page(
        slug=f"topic-{os.urandom(2).hex()}",
        title="User prefers concise answers",
        body="User always asks for bullet points. Avoid long prose.",
        summary="Concise answer style preference",
        tags=["style"],
        importance=0.9,
        evidence_ids=["m1", "m2"],
    )
    store.upsert_wiki_page(
        slug=f"topic2-{os.urandom(2).hex()}",
        title="User uses dark mode after 8pm",
        body="Auto-switch theme based on local sunset.",
        summary="Theme switching rule",
        tags=["theme"],
        importance=0.6,
        evidence_ids=["m3"],
    )
    return page["id"]


class WikiExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)
        self.pid = _seed_wiki(self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_includes_all_pages(self):
        from fastapi.testclient import TestClient

        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.get("/api/wiki/export")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(data["count"], 2)
            self.assertIn("User prefers concise answers", data["markdown"])
            self.assertIn("evidence", data["markdown"])

    def test_export_query_filter(self):
        from fastapi.testclient import TestClient

        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.get("/api/wiki/export", params={"q": "dark mode"})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["count"], 1)
            self.assertIn("dark mode", data["markdown"])

    def test_page_export_returns_context_block(self):
        from fastapi.testclient import TestClient

        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.get(f"/api/wiki/{self.pid}/export")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertIn("User prefers concise answers", data["markdown"])
            self.assertIn("background context", data["context"])

    def test_ask_returns_top_matches(self):
        from fastapi.testclient import TestClient

        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post("/api/wiki/ask", params={"q": "concise answers"})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(len(data["matches"]), 1)
            self.assertIn("User prefers concise answers", data["context"])

    def test_ask_no_match(self):
        from fastapi.testclient import TestClient

        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post("/api/wiki/ask", params={"q": "completely-unrelated-xyz"})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data["matches"], [])
            self.assertIn("no wiki pages matched", data["context"])

    def test_ask_requires_q(self):
        from fastapi.testclient import TestClient

        from loop_memory.serve.app import create_app
        app = create_app(self.store)
        with TestClient(app) as c:
            r = c.post("/api/wiki/ask", params={"q": ""})
            self.assertEqual(r.status_code, 400)


class ConsolidateNowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_endpoint_returns_queued_when_scheduler_present(self):
        from fastapi.testclient import TestClient

        from loop_memory.jobs.scheduler import ConsolidatorScheduler
        from loop_memory.serve.app import create_app

        scheduler = ConsolidatorScheduler(self.store)
        app = create_app(self.store, scheduler=scheduler)
        with TestClient(app) as c:
            r = c.post("/api/admin/consolidate-now")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            # scheduler.run_now(block=False) returns None → queued
            self.assertTrue(data.get("queued"))
        scheduler.stop()


class CliExportAskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        self.store = MemoryStore(self.db)
        _seed_wiki(self.store)
        os.environ["LOOP_MEMORY_DB"] = self.db

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("LOOP_MEMORY_DB", None)

    def test_cli_ask_prints_block(self):
        import contextlib
        import io

        from loop_memory.cli import main as cli_main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_main.main(["ask", "concise answers"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("User prefers concise answers", out)
        self.assertIn("Distilled knowledge", out)
