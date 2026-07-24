"""Tests for the MCP server's JSON-RPC tool handlers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="loop_mcp_")
        self.db = Path(self.tmp) / "mcp.db"
        self.prev_db = os.environ.get("LOOP_MEMORY_DB")
        os.environ["LOOP_MEMORY_DB"] = str(self.db)
        # Seed a wiki page so list_wiki has something to return
        from loop_memory.storage.sqlite_store import MemoryStore
        self.store = MemoryStore(self.db)
        self.store.upsert_wiki_page(
            slug="test-knowledge", title="Test Knowledge",
            body="This is the body of a test wiki page with detailed content.",
            summary="A summary of test knowledge.",
            tags=["test", "demo"], importance=0.7,
        )

    def tearDown(self) -> None:
        if self.prev_db is None:
            os.environ.pop("LOOP_MEMORY_DB", None)
        else:
            os.environ["LOOP_MEMORY_DB"] = self.prev_db

    def _handle(self, method, params=None, rid=1):
        from loop_memory.mcp import _handle
        return _handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def test_initialize(self):
        r = self._handle("initialize")
        self.assertEqual(r["id"], 1)
        self.assertEqual(r["result"]["serverInfo"]["name"], "loop-memory")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_ping(self):
        r = self._handle("ping")
        self.assertEqual(r["id"], 1)

    def test_tools_list(self):
        r = self._handle("tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertIn("recall", names)
        self.assertIn("list_wiki", names)
        self.assertIn("get_wiki", names)
        self.assertIn("wiki_summary", names)

    def test_list_wiki_returns_seeded_page(self):
        r = self._handle("tools/call", {"name": "list_wiki", "arguments": {}})
        content = r["result"]["content"]
        self.assertEqual(len(content), 1)
        self.assertIn("test-knowledge", content[0]["text"])
        self.assertIn("Test Knowledge", content[0]["text"])

    def test_get_wiki_by_slug(self):
        r = self._handle("tools/call", {"name": "get_wiki", "arguments": {"slug": "test-knowledge"}})
        text = r["result"]["content"][0]["text"]
        self.assertIn("Test Knowledge", text)
        self.assertIn("body of a test wiki page", text)

    def test_get_wiki_unknown_slug(self):
        r = self._handle("tools/call", {"name": "get_wiki", "arguments": {"slug": "nope"}})
        text = r["result"]["content"][0]["text"]
        self.assertIn("No wiki page found", text)

    def test_recall_no_query_is_error(self):
        r = self._handle("tools/call", {"name": "recall", "arguments": {}})
        text = r["result"]["content"][0]["text"]
        self.assertIn("missing", text)

    def test_unknown_tool_returns_error(self):
        r = self._handle("tools/call", {"name": "no_such_tool", "arguments": {}})
        self.assertIn("error", r)
        self.assertEqual(r["error"]["code"], -32601)

    def test_notification_has_no_response(self):
        r = self._handle("notifications/initialized")
        self.assertIsNone(r)

    def test_wiki_summary_includes_seeded(self):
        r = self._handle("tools/call", {"name": "wiki_summary", "arguments": {}})
        text = r["result"]["content"][0]["text"]
        # The summary lists page titles (not raw slugs).
        self.assertIn("Test Knowledge", text)
        self.assertIn("summary of test knowledge", text)

    def test_serve_stdio_round_trip(self):
        """Smoke test: pipe a couple of messages into serve_stdio."""
        import io

        from loop_memory.mcp import serve_stdio
        msgs = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "list_wiki", "arguments": {}}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),  # no id
            "",  # blank line should be ignored
        ]
        import sys
        stdin_bak = sys.stdin
        stdout_bak = sys.stdout
        sys.stdin = io.StringIO("\n".join(msgs) + "\n")
        sys.stdout = io.StringIO()
        try:
            serve_stdio()
            captured = io.StringIO(sys.stdout.getvalue())
        finally:
            sys.stdin = stdin_bak
            sys.stdout = stdout_bak
        out_lines = [ln for ln in captured.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(out_lines), 2, f"expected 2 responses, got: {captured.getvalue()}")
        for ln in out_lines:
            obj = json.loads(ln)
            self.assertIn("jsonrpc", obj)
            self.assertEqual(obj["jsonrpc"], "2.0")


class InstallHooksUpsertTests(unittest.TestCase):
    """The TOML upsert helper must never duplicate the block on rerun."""

    def setUp(self) -> None:
        from loop_memory.cli.main import _upsert_block
        self._upsert = _upsert_block

    def test_first_run_appends(self):
        text = "# user config\nother_setting = 1\n"
        block = (
            "\n# [loop-memory] auto-installed by `loop-memory install-hooks`.\n"
            "[mcp_servers.loop_memory]\n"
            "command = \"loop-memory\"\n"
            "args = [\"mcp\"]\n"
            "\n"
            "[[hooks]]\n"
            "event = \"session.start\"\n"
            "command = \"loop-memory inject\"\n"
        )
        result = self._upsert(text, block, "# [loop-memory]")
        self.assertIn("# [loop-memory]", result)
        self.assertIn("[mcp_servers.loop_memory]", result)
        self.assertIn("[[hooks]]", result)
        # No duplication after first run
        self.assertEqual(result.count("[mcp_servers.loop_memory]"), 1)
        self.assertEqual(result.count("[[hooks]]"), 1)

    def test_second_run_is_idempotent(self):
        block = (
            "\n# [loop-memory] auto-installed by `loop-memory install-hooks`.\n"
            "[mcp_servers.loop_memory]\n"
            "command = \"loop-memory\"\n"
            "args = [\"mcp\"]\n"
            "\n"
            "[[hooks]]\n"
            "event = \"session.start\"\n"
            "command = \"loop-memory inject\"\n"
        )
        text = "# user header\n" + block.lstrip("\n")
        # 3 reruns in a row
        for _ in range(3):
            text = self._upsert(text, block, "# [loop-memory]")
        # Block is present exactly once
        self.assertEqual(text.count("[mcp_servers.loop_memory]"), 1)
        self.assertEqual(text.count("[[hooks]]"), 1)
        # User config preserved
        self.assertIn("# user header", text)

    def test_user_section_after_block_is_kept(self):
        block = (
            "\n# [loop-memory] auto-installed by `loop-memory install-hooks`.\n"
            "[mcp_servers.loop_memory]\n"
            "command = \"loop-memory\"\n"
            "args = [\"mcp\"]\n"
        )
        text = (
            "# user header\n"
            + block.lstrip("\n")
            + "\n\n# user section below\n"
            "[other_tool]\n"
            "enabled = true\n"
        )
        result = self._upsert(text, block, "# [loop-memory]")
        # The user section after the block must still be there
        self.assertIn("[other_tool]", result)
        self.assertIn("enabled = true", result)
        # And the block appears exactly once
        self.assertEqual(result.count("[mcp_servers.loop_memory]"), 1)


class InjectOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="loop_inject_")
        self.db = Path(self.tmp) / "inject.db"
        self.prev_db = os.environ.get("LOOP_MEMORY_DB")
        os.environ["LOOP_MEMORY_DB"] = str(self.db)
        from loop_memory.storage.sqlite_store import MemoryStore
        s = MemoryStore(self.db)
        s.upsert_wiki_page(
            slug="alpha", title="Alpha",
            body="A body.", summary="A short summary.",
            tags=["alpha"], importance=0.7,
        )
        s.upsert_wiki_page(
            slug="beta", title="Beta",
            body="B body.", summary="", tags=[], importance=0.5,
        )

    def tearDown(self) -> None:
        if self.prev_db is None:
            os.environ.pop("LOOP_MEMORY_DB", None)
        else:
            os.environ["LOOP_MEMORY_DB"] = self.prev_db

    def test_inject_emits_wiki_block(self):
        import io
        import sys

        from loop_memory.cli.main import cmd_inject
        bak_out = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = cmd_inject([])
        finally:
            out = sys.stdout.getvalue()
            sys.stdout = bak_out
        self.assertEqual(rc, 0)
        self.assertIn("# Long-term memory context", out)
        self.assertIn("Alpha", out)
        self.assertIn("`alpha`", out)
        self.assertIn("A short summary.", out)
        # The body should be used when summary is empty
        self.assertIn("B body.", out)



class McpWriteToolTests(unittest.TestCase):
    """Cover the write surface added to the stdio MCP server so any
    MCP-aware client (Codex / Claude / Hermes / …) can remember,
    forget, and feedback via the same JSON-RPC transport as the
    existing read-only tools.
    """

    def setUp(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from loop_memory.storage.sqlite_store import MemoryStore
        self.tmp = Path(tempfile.mkdtemp(prefix="loop_mcp_w_"))
        self.db = self.tmp / "mcp_w.db"
        self.prev_db = os.environ.get("LOOP_MEMORY_DB")
        self.prev_agent = os.environ.get("LOOP_MEMORY_AGENT_ID")
        os.environ["LOOP_MEMORY_DB"] = str(self.db)
        os.environ["LOOP_MEMORY_AGENT_ID"] = "mcp-test-agent"
        # Seed a page so recall has something to fetch
        s = MemoryStore(self.db)
        s.upsert_wiki_page(
            slug="mcp-test-knowledge", title="MCP Test Knowledge",
            body="body of a test wiki page", summary="summary of mcp test knowledge",
            tags=["mcp"], importance=0.6,
        )

    def tearDown(self) -> None:
        import shutil
        if self.prev_db is None:
            import os
            os.environ.pop("LOOP_MEMORY_DB", None)
        else:
            import os
            os.environ["LOOP_MEMORY_DB"] = self.prev_db
        if self.prev_agent is None:
            import os
            os.environ.pop("LOOP_MEMORY_AGENT_ID", None)
        else:
            import os
            os.environ["LOOP_MEMORY_AGENT_ID"] = self.prev_agent
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, name, arguments):
        import json
        from loop_memory.mcp import TOOL_DISPATCH
        result = TOOL_DISPATCH[name](arguments)
        self.assertEqual(len(result), 1)
        return result[0]["text"]

    def test_tools_list_contains_writes(self):
        from loop_memory.mcp import TOOLS
        names = {t["name"] for t in TOOLS}
        self.assertIn("remember", names)
        self.assertIn("forget", names)
        self.assertIn("feedback", names)

    def test_remember_round_trip(self):
        out = self._call("remember", {
            "text": "the deploys run on Fridays at 17:00 UTC",
            "kind": "fact",
            "importance": 0.7,
            "tags": ["ops"],
            "external_id": "deploy-window",
        })
        self.assertIn("remembered", out)
        # The row exists with the right external_id and is findable
        from loop_memory.storage.sqlite_store import MemoryStore
        s = MemoryStore(self.db)
        row = s.find_memory_by_external_id("mcp-test-agent", "deploy-window")
        self.assertIsNotNone(row)
        self.assertEqual(row.importance, 0.7)
        self.assertIn("ops", row.tags)

    def test_remember_is_idempotent(self):
        a = self._call("remember", {"text": "v1", "external_id": "k"})
        b = self._call("remember", {"text": "v2", "external_id": "k"})
        self.assertIn("remembered", a)
        self.assertIn("remembered", b)
        from loop_memory.storage.sqlite_store import MemoryStore
        s = MemoryStore(self.db)
        rows = s.list_memories(external_id="k")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].text, "v2")

    def test_remember_rejects_empty_text(self):
        out = self._call("remember", {"text": "  "})
        self.assertIn("missing", out)

    def test_forget_by_external_id(self):
        self._call("remember", {"text": "x", "external_id": "to-forget"})
        out = self._call("forget", {"external_id": "to-forget"})
        self.assertIn("deleted=1", out)
        out = self._call("forget", {"external_id": "to-forget"})
        self.assertIn("No memory matches", out)

    def test_feedback_up_then_ignore(self):
        self._call("remember", {"text": "y", "external_id": "fb"})
        out = self._call("feedback", {"value": "up", "external_id": "fb"})
        self.assertIn("feedback(up)", out)
        out = self._call("feedback", {"value": "ignore", "external_id": "fb"})
        self.assertIn("deleted=1", out)

    def test_feedback_rejects_unknown_value(self):
        self._call("remember", {"text": "z", "external_id": "fb2"})
        out = self._call("feedback", {"value": "thumb", "external_id": "fb2"})
        self.assertIn("up|down|ignore", out)

    def test_recall_after_remember(self):
        self._call("remember", {
            "text": "we use Postgres for orders",
            "kind": "fact",
            "tags": ["infra"],
            "external_id": "orders-db",
        })
        from loop_memory.mcp import TOOL_DISPATCH
        out = TOOL_DISPATCH["recall"]({"query": "Postgres", "limit": 5})
        self.assertEqual(len(out), 1)
        self.assertIn("Postgres", out[0]["text"])

    def test_serve_stdio_includes_remember(self):
        """End-to-end: pipe a remember() through the stdio server."""
        import io
        import json
        import sys

        from loop_memory.mcp import serve_stdio
        msgs = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "remember", "arguments": {
                            "text": "End-to-end MCP remember", "external_id": "e2e-1",
                        }}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        ]
        stdin_bak, stdout_bak = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("\n".join(msgs) + "\n")
        sys.stdout = io.StringIO()
        try:
            serve_stdio()
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = stdin_bak, stdout_bak
        responses = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(responses), 2)
        self.assertIn("remembered", responses[1]["result"]["content"][0]["text"])
