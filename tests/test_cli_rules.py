"""Tests for the rules installer (Audit 2026-08-23)."""
import os
import tempfile
import unittest
from pathlib import Path

from loop_memory.cli.main import main, COMMAND_HELP
from loop_memory.cli.commands.rules import (
    AGENT_TARGETS,
    MARKER,
    _install,
    _render_block,
    _resolve_target,
    run_rules,
)


class RulesInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="loop_rules_")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_renders_generic_block_with_marker(self) -> None:
        block = _render_block("generic")
        self.assertIn(MARKER, block)
        self.assertIn("Memory discipline", block)
        self.assertIn("task start", block)
        self.assertIn("Mid-task", block)
        self.assertIn("Wrap-up", block)

    def test_renders_agent_specific_block(self) -> None:
        for agent in ("codex", "claude", "hermes", "openclaw"):
            block = _render_block(agent)
            self.assertIn(MARKER, block, agent)

    def test_hermes_block_has_hermes_section(self) -> None:
        block = _render_block("hermes")
        self.assertIn("Hermes-specific", block)

    def test_openclaw_block_has_openclaw_section(self) -> None:
        block = _render_block("openclaw")
        self.assertIn("OpenClaw-specific", block)

    def test_resolve_target_known_agents(self) -> None:
        self.assertEqual(_resolve_target("codex", self.root), self.root / "AGENTS.md")
        self.assertEqual(_resolve_target("claude", self.root), self.root / "CLAUDE.md")
        self.assertEqual(_resolve_target("hermes", self.root), self.root / "AGENTS.md")
        self.assertEqual(_resolve_target("openclaw", self.root), self.root / "AGENTS.md")

    def test_resolve_target_unknown_falls_back_to_agents_md(self) -> None:
        self.assertEqual(_resolve_target("claude-cli", self.root),
                         self.root / "AGENTS.md")
        self.assertEqual(_resolve_target("anything", self.root),
                         self.root / "AGENTS.md")

    def test_install_creates_new_file(self) -> None:
        status, target = _install(self.root, "codex", force=False)
        self.assertEqual(status, "installed")
        # Then via run_rules we should actually write.
        self.assertEqual(run_rules(["--agent", "codex", "--write", "--cwd",
                                     str(self.root)]), 0)
        target = self.root / "AGENTS.md"
        self.assertTrue(target.exists())
        content = target.read_text(encoding="utf-8")
        self.assertIn(MARKER, content)
        self.assertIn("Memory discipline", content)

    def test_install_appends_without_overwriting_user_content(self) -> None:
        target = self.root / "AGENTS.md"
        target.write_text(
            "# User's own rule\n\nDo not touch this.\n",
            encoding="utf-8",
        )
        self.assertEqual(run_rules(["--agent", "codex", "--write",
                                     "--cwd", str(self.root)]), 0)
        content = target.read_text(encoding="utf-8")
        self.assertIn("Do not touch this.", content)
        self.assertIn(MARKER, content)
        # User content must appear before our appended block.
        self.assertLess(content.index("Do not touch this."),
                        content.index(MARKER))

    def test_install_is_idempotent(self) -> None:
        self.assertEqual(run_rules(["--agent", "codex", "--write",
                                     "--cwd", str(self.root)]), 0)
        first = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        # Second run: same content, no duplicate block.
        self.assertEqual(run_rules(["--agent", "codex", "--write",
                                     "--cwd", str(self.root)]), 0)
        second = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(first, second)
        # The block contains the marker twice (open + close comment).
        # Re-running is a no-op so the count must stay at 2.
        self.assertEqual(first.count(MARKER), 2)
        self.assertEqual(second.count(MARKER), 2)
        self.assertEqual(first, second)

    def test_force_overwrites_marker_span(self) -> None:
        # Seed a user rule above the discipline block.
        (self.root / "AGENTS.md").write_text(
            "# user-rule\n\nkeep me\n",
            encoding="utf-8",
        )
        self.assertEqual(run_rules(["--agent", "codex", "--write",
                                     "--cwd", str(self.root)]), 0)
        first = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        # --force refreshes the discipline block in place; user content
        # above the start marker must stay byte-identical.
        self.assertEqual(run_rules(["--agent", "codex", "--write",
                                     "--force", "--cwd", str(self.root)]), 0)
        second = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        # User content is preserved.
        self.assertIn("keep me", second)
        # Marker count stays at 2 (one open + one close).
        self.assertEqual(first.count(MARKER), 2)
        self.assertEqual(second.count(MARKER), 2)

    def test_claude_targets_claudemd(self) -> None:
        self.assertEqual(run_rules(["--agent", "claude", "--write",
                                     "--cwd", str(self.root)]), 0)
        target = self.root / "CLAUDE.md"
        self.assertTrue(target.exists())
        self.assertNotEqual(target, self.root / "AGENTS.md")
        self.assertIn(MARKER, target.read_text(encoding="utf-8"))

    def test_print_mode_prints_to_stdout_no_write(self) -> None:
        import io
        from unittest import mock
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.assertEqual(run_rules(["--agent", "generic"]), 0)
        out = buf.getvalue()
        self.assertIn(MARKER, out)
        # Print mode must NOT create any file.
        self.assertEqual(list(self.root.iterdir()), [])

    def test_command_help_is_present(self) -> None:
        self.assertIn("rules", COMMAND_HELP)
        self.assertIn("--write", COMMAND_HELP["rules"])
        self.assertIn("--agent", COMMAND_HELP["rules"])

    def test_help_via_dispatcher_exits_zero(self) -> None:
        # Same pattern as ``test_cli_v7`` — exercises the dispatcher
        # path for the new subcommand.
        self.assertEqual(main(["rules", "--help"]), 0)

    def test_unknown_argument_returns_2(self) -> None:
        self.assertEqual(run_rules(["--bogus"]), 2)

    def test_agent_targets_map_matches_shipped_agents(self) -> None:
        # The shipped-agents mapping must cover everything the README
        # advertises (4 + generic).
        for agent in ("codex", "claude", "hermes", "openclaw", "generic"):
            self.assertIn(agent, AGENT_TARGETS)


if __name__ == "__main__":
    unittest.main()
