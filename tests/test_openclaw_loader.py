"""Tests for the rewritten OpenClawLoader that handles real clawx
session format (type=session + type=message + content[] parts)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from loop_memory.ingest.loader import OpenClawLoader


def _write_session_jsonl(path: Path, lines: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")


class OpenClawLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions_dir = self.tmp / "agents" / "main" / "sessions"
        self.sessions_dir.mkdir(parents=True)
        self.loader = OpenClawLoader()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_real_format(self, name: str) -> Path:
        path = self.sessions_dir / name
        lines = [
            {"type": "session", "id": name.replace(".jsonl", ""),
             "timestamp": "2026-05-19T04:45:46.750Z",
             "cwd": "/Users/example/.openclaw/workspace", "version": "3"},
            {"type": "model_change", "id": "m1", "provider": "minimax-portal",
             "modelId": "MiniMax-M2.7", "timestamp": "2026-05-19T04:45:46.786Z"},
            {"type": "message", "id": "msg1",
             "timestamp": "2026-05-19T04:45:46.823Z",
             "message": {"role": "user", "timestamp": 1779165946820,
                         "content": [{"type": "text",
                                      "text": "请立即生成A股早盘分析报告。"}]}},
            {"type": "message", "id": "msg2",
             "timestamp": "2026-05-19T04:45:54.976Z",
             "message": {"role": "assistant", "timestamp": 1779165954976,
                         "content": [{"type": "thinking",
                                      "thinking": "Let me check the indices first."},
                                     {"type": "toolCall", "id": "c1",
                                      "name": "exec",
                                      "arguments": {"command": "ls /tmp"}}]}},
            {"type": "message", "id": "msg3",
             "timestamp": "2026-05-19T04:46:00.000Z",
             "message": {"role": "toolresult",
                         "toolCallId": "c1", "toolName": "exec",
                         "content": [{"type": "text",
                                      "text": "file1\nfile2"}]}},
            {"type": "message", "id": "msg4",
             "timestamp": "2026-05-19T04:46:15.000Z",
             "message": {"role": "assistant", "timestamp": 1779165975000,
                         "content": [{"type": "text",
                                      "text": "Here is the A-share morning report."}]}},
        ]
        _write_session_jsonl(path, lines)
        return path

    def test_discovers_real_format(self):
        self._write_real_format("abc-123.jsonl")
        # Also drop a companion trajectory file which should be ignored
        _write_session_jsonl(
            self.sessions_dir / "abc-123.trajectory.jsonl",
            [{"type": "session.started", "ts": "2026-05-19T04:45:46.750Z"}],
        )
        _write_session_jsonl(
            self.sessions_dir / "abc-123.checkpoint.xyz.jsonl",
            [{"type": "checkpoint", "data": {}}],
        )
        files = list(self.loader.discover(self.tmp))
        names = [p.name for p in files]
        self.assertIn("abc-123.jsonl", names)
        self.assertNotIn("abc-123.trajectory.jsonl", names)
        self.assertNotIn("abc-123.checkpoint.xyz.jsonl", names)
        # sessions.json is metadata, should be skipped
        (self.sessions_dir / "sessions.json").write_text("{}")
        files = list(self.loader.discover(self.tmp))
        self.assertNotIn("sessions.json", [p.name for p in files])

    def test_parses_real_format(self):
        path = self._write_real_format("session-a.jsonl")
        sess = self.loader.load_one(path)
        self.assertIsNotNone(sess)
        self.assertEqual(sess.source, "openclaw")
        self.assertEqual(sess.external_id, "session-a")
        self.assertEqual(len(sess.turns), 4, "should keep 4 message turns")
        # Roles in order
        self.assertEqual([t.role for t in sess.turns],
                         ["user", "assistant", "toolresult", "assistant"])
        # Toolcall turned into a one-liner
        assistant_turn = sess.turns[1]
        self.assertIn("[thinking]", assistant_turn.text)
        self.assertIn("[toolCall:exec]", assistant_turn.text)
        # Timestamps parsed
        self.assertGreater(sess.started_at, 0)
        self.assertGreater(sess.ended_at, sess.started_at)
        # CWD attached to title
        self.assertIn("/Users/example/.openclaw/workspace", sess.title or "")

    def test_markdown_daily_log(self):
        # clawx writes daily memory journals as markdown
        ws_memory = self.tmp / "workspace" / "memory"
        ws_memory.mkdir(parents=True)
        md_path = ws_memory / "2026-05-31.md"
        md_path.write_text(
            "# 2026-05-31 Daily Log\n\n## Project progress\n\n"
            "Fixed CSS layout bug. CSS went from 35 bytes to 132KB.\n",
            encoding="utf-8",
        )
        files = list(self.loader.discover(self.tmp))
        names = [p.name for p in files]
        self.assertIn("2026-05-31.md", names)
        sess = self.loader.load_one(md_path)
        self.assertIsNotNone(sess)
        self.assertEqual(len(sess.turns), 1)
        self.assertEqual(sess.turns[0].role, "reflection")
        self.assertIn("Fixed CSS", sess.turns[0].text)

    def test_skips_vendor_dirs(self):
        # Build a fake workspace/memory nested next to vendor code;
        # the vendor code's .jsonl files must NOT be picked up.
        vendor = self.tmp / "workspace" / "node_modules" / "fake" / "data"
        vendor.mkdir(parents=True)
        (vendor / "package.jsonl").write_text('{"role":"x","content":"vendor noise"}')
        files = list(self.loader.discover(self.tmp))
        for f in files:
            self.assertNotIn("node_modules", str(f))

    def test_legacy_flat_jsonl_still_works(self):
        path = self.sessions_dir / "legacy.jsonl"
        _write_session_jsonl(path, [
            {"role": "user", "content": "hello", "ts": 1700000000.0},
            {"role": "assistant", "content": "hi", "ts": 1700000001.0},
        ])
        sess = self.loader.load_one(path)
        self.assertIsNotNone(sess)
        self.assertEqual(len(sess.turns), 2)
        self.assertEqual(sess.turns[0].role, "user")


if __name__ == "__main__":
    unittest.main()
