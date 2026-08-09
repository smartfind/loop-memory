"""Regression tests for the ``loop-memory --version`` family of flags.

Before this fix, ``loop-memory --version`` returned ``unknown command:
--version`` which tripped users on the most basic operation a CLI
must support. These tests pin all three forms (``--version``, ``-V``,
``version``) and assert the printed value matches the installed
distribution's metadata.
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from importlib import metadata as importlib_metadata

from loop_memory.cli.main import main


def _run(args: list[str]) -> tuple[int, str]:
    """Run ``loop-memory <args>`` and return ``(rc, stdout)``."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(args)
    return rc, buf.getvalue()


class CliVersionTests(unittest.TestCase):
    def test_dash_dash_version_prints_distribution_version(self) -> None:
        expected = importlib_metadata.version("loop-memory")
        rc, out = _run(["--version"])
        self.assertEqual(rc, 0, out)
        self.assertIn(expected, out.strip(), out)
        # The output should be human-friendly: at minimum include the package name.
        self.assertIn("loop-memory", out)

    def test_dash_V_short_flag_also_works(self) -> None:
        rc, out = _run(["-V"])
        self.assertEqual(rc, 0, out)
        self.assertIn(importlib_metadata.version("loop-memory"), out.strip(), out)

    def test_version_subcommand_also_works(self) -> None:
        rc, out = _run(["version"])
        self.assertEqual(rc, 0, out)
        self.assertIn(importlib_metadata.version("loop-memory"), out.strip(), out)

    def test_version_does_not_require_a_database(self) -> None:
        """``version`` must work even when ``$LOOP_MEMORY_DB`` points
        somewhere nonexistent — it is a metadata-only probe.
        """
        # Save and unset
        prev = sys.modules.pop("loop_memory.storage.sqlite_store", None)
        try:
            rc, out = _run(["version"])
            self.assertEqual(rc, 0, out)
        finally:
            if prev is not None:
                sys.modules["loop_memory.storage.sqlite_store"] = prev





class CliSubcommandHelpTests(unittest.TestCase):
    """Pin dispatcher behaviour for ``<subcommand> --help`` / ``-h``.

    The install smoke test caught three real bugs against the
    published 0.4.0 wheel:

    * ``ingest --help`` crashed with ``ValueError: unknown source:
      '--help'`` because the handler treated ``--help`` as a source name.
    * ``serve --help`` crashed with ``ModuleNotFoundError: No module
      named 'fastapi'`` because the [serve] extra wasn't installed
      (the handler lazy-imports fastapi). Users on a base install
      couldn't read serve's help text.
    * ``hook --help`` returned rc=2 from the ``die()`` helper even
      though it printed the correct usage line.

    The dispatcher now intercepts ``-h`` / ``--help`` AFTER the
    subcommand name and prints a static help line, never invoking
    the handler — so all three forms exit cleanly with rc=0 even on
    a ``pip install loop-memory`` (no [serve] extra) install.
    """

    def _run(self, args: list[str]) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(args)
        return rc, buf.getvalue()

    def test_ingest_help_does_not_crash_with_value_error(self) -> None:
        rc, out = self._run(["ingest", "--help"])
        self.assertEqual(rc, 0, out)
        self.assertIn("ingest", out.lower())

    def test_serve_help_works_without_serve_extra(self) -> None:
        """Must not require the [serve] extra to be installed."""
        rc, out = self._run(["serve", "--help"])
        self.assertEqual(rc, 0, out)
        self.assertIn("serve", out.lower())

    def test_hook_help_returns_zero(self) -> None:
        rc, out = self._run(["hook", "--help"])
        self.assertEqual(rc, 0, out)
        self.assertIn("hook", out.lower())

    def test_short_form_dash_h_also_works(self) -> None:
        rc, _ = self._run(["ingest", "-h"])
        self.assertEqual(rc, 0)

    def test_unknown_subcommand_still_returns_2(self) -> None:
        """Make sure the help interceptor doesn't swallow real
        unknown-command errors.
        """
        rc, _ = self._run(["totally-not-a-command", "--help"])
        self.assertEqual(rc, 2)

    def test_every_registered_subcommand_has_help_text(self) -> None:
        """Every COMMANDS entry MUST have a corresponding help string
        so a fresh user can ``--help`` any subcommand without surprises.
        """
        from loop_memory.cli import main as cli_main
        missing = sorted(c for c in cli_main.COMMANDS if c not in cli_main.COMMAND_HELP)
        self.assertEqual(missing, [], f"missing COMMAND_HELP for: {missing}")


if __name__ == "__main__":
    unittest.main()
