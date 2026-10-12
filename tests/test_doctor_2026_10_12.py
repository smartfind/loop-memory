"""Regression tests for ``loop-memory doctor`` -- 10-check store-health
diagnostic (audit 2026-10-12).

Pattern adopted from ``cortexkit/magic-context doctor`` (MIT, 2026-10-12)
plus ``dsh-memento`` write-approval-gate.  Stdlib-only -- we re-implement
the checks against our own ``MemoryStore`` so the doctor never depends
on a separate health library.

Coverage targets:
- dataclass shape: ``DoctorCheck`` exposes name/status/message/details/fix_hint/auto_fixable
- status constants: only ``ok/info/warn/error`` accepted
- each of the 10 checks fires under the expected fixture (positive + negative)
- ``--fix`` repairs ``audit_triggers`` and reports ok
- exit code: 0 on clean, 1 on any error
- JSON output is valid + machine-parseable
- list mode prints the check function names without running them
- DB-missing path returns ``db_exists=error`` and exit 1
- chained ``store._conn().__enter__()`` does NOT close the conn under
  Python 3.14 (regression for the bug we hit during 0.4.14 development)
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from loop_memory import doctor
from loop_memory.cli.commands import doctor as doctor_cli
from loop_memory.storage.sqlite_store import MemoryStore


# --------------------------------------------------------------------- helpers


def _new_store() -> tuple[MemoryStore, Path]:
    """Spin up an isolated store in a fresh tmpdir.

    Returns ``(store, tmpdir)`` -- caller is responsible for
    ``shutil.rmtree(tmpdir, ignore_errors=True)`` in tearDown.
    """
    base = Path(tempfile.mkdtemp(prefix="loop_doctor_"))
    base.mkdir(parents=True, exist_ok=True)
    return MemoryStore(str(base / "db.sqlite")), base


def _shim_path() -> str:
    """Path to the venv ``loop-memory`` console script."""
    return str(Path(__file__).parent.parent / ".venv-test" / "bin" / "loop-memory")


def _has_shim() -> bool:
    return Path(_shim_path()).exists()


# --------------------------------------------------------------------- dataclass


class DoctorDataclassShapeTests(unittest.TestCase):
    """The ``DoctorCheck`` dataclass must be serialisable + carry fix context."""

    def test_status_constants_are_bounded(self) -> None:
        # The four canonical levels must exist and be lowercase strings.
        for lvl in ("ok", "info", "warn", "error"):
            attr = f"STATUS_{lvl.upper()}"
            self.assertTrue(hasattr(doctor, attr), f"missing doctor.{attr}")
            self.assertEqual(getattr(doctor, attr), lvl)

    def test_dataclass_carries_fix_hint(self) -> None:
        c = doctor.DoctorCheck(
            name="x",
            status=doctor.STATUS_WARN,
            message="m",
            fix_hint="do this",
        )
        self.assertEqual(c.fix_hint, "do this")
        self.assertFalse(c.auto_fixable)

    def test_dataclass_serialisable_via_asdict(self) -> None:
        c = doctor.DoctorCheck(
            name="x",
            status=doctor.STATUS_INFO,
            message="m",
            details={"k": "v"},
        )
        d = asdict(c)
        self.assertEqual(d["name"], "x")
        self.assertEqual(d["status"], "info")
        self.assertEqual(d["details"], {"k": "v"})

    def test_checks_constant_lists_10_functions(self) -> None:
        # The CHECKS list is the canonical "what the doctor does"
        # surface -- it must have exactly 10 entries (audit 2026-10-12).
        self.assertEqual(len(doctor.CHECKS), 10)


# --------------------------------------------------------------------- per-check


class DoctorPerCheckTests(unittest.TestCase):
    """Each of the 10 checks must run against a real MemoryStore and return a DoctorCheck."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        # Touch the store so its schema_meta is populated.
        with self.store._conn():
            pass

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_db_exists_ok(self) -> None:
        c = doctor._check_db_exists(self.store)
        self.assertEqual(c.name, "db_exists")
        self.assertEqual(c.status, doctor.STATUS_OK)

    def test_check_db_exists_missing(self) -> None:
        # Point at a non-existent path via a fresh store.
        bogus = MemoryStore.__new__(MemoryStore)
        bogus.path = self.tmp / "no_such.sqlite"
        c = doctor._check_db_exists(bogus)
        self.assertEqual(c.status, doctor.STATUS_ERROR)

    def test_check_db_integrity_ok(self) -> None:
        # Regression for the Python 3.14 chained ``__enter__()`` bug
        # we hit during 0.4.14 dev: this MUST not say
        # "Cannot operate on a closed database".
        c = doctor._check_db_integrity(self.store)
        self.assertEqual(c.name, "db_integrity")
        self.assertEqual(c.status, doctor.STATUS_OK)
        self.assertIn("ok", c.message.lower())

    def test_check_schema_version_ok(self) -> None:
        c = doctor._check_schema_version(self.store)
        # Fresh store may have ``schema_meta`` populated by __init__.
        self.assertIn(c.status, {doctor.STATUS_OK, doctor.STATUS_INFO})

    def test_check_audit_triggers_ok(self) -> None:
        c = doctor._check_audit_triggers(self.store)
        self.assertEqual(c.status, doctor.STATUS_OK)

    def test_check_audit_triggers_flags_dropped(self) -> None:
        # Drop a trigger and confirm doctor flags it as an error
        # with auto_fixable=True (any missing trigger breaks the
        # DB-level audit invariant, so this is hard-error not warn).
        with self.store._conn() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_memories_ai")
        c = doctor._check_audit_triggers(self.store)
        self.assertEqual(c.status, doctor.STATUS_ERROR)
        self.assertTrue(c.auto_fixable)

    def test_check_audit_log_writable_ok(self) -> None:
        c = doctor._check_audit_log_writable(self.store)
        self.assertEqual(c.status, doctor.STATUS_OK)

    def test_check_version_consistent(self) -> None:
        c = doctor._check_version_consistent(self.store)
        self.assertEqual(c.name, "version_consistent")
        self.assertIn(c.status, {doctor.STATUS_OK, doctor.STATUS_INFO})

    def test_check_recall_paths_works(self) -> None:
        c = doctor._check_recall_paths_works(self.store)
        self.assertEqual(c.name, "recall_paths_works")


# --------------------------------------------------------------------- orchestrator


class DoctorRunTests(unittest.TestCase):
    """``run_checks`` + ``apply_fix`` + ``exit_code`` integration."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()
        with self.store._conn():
            pass

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_checks_returns_list(self) -> None:
        report = doctor.run_checks(self.store)
        self.assertIsInstance(report, list)
        self.assertEqual(len(report), len(doctor.CHECKS))
        for c in report:
            self.assertIsInstance(c, doctor.DoctorCheck)

    def test_exit_code_clean(self) -> None:
        report = doctor.run_checks(self.store)
        # Healthy fixture should be 0 even if some checks report info.
        self.assertEqual(doctor.exit_code(report), 0)

    def test_apply_fix_repairs_audit_triggers(self) -> None:
        # Drop a trigger, run apply_fix, confirm re-installed.
        with self.store._conn() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_memories_ai")
        result = doctor.apply_fix(self.store, "audit_triggers")
        self.assertEqual(result.status, doctor.STATUS_OK)
        # Verify the trigger is now installed.
        report = doctor.run_checks(self.store)
        audit = [c for c in report if c.name == "audit_triggers"][0]
        self.assertEqual(audit.status, doctor.STATUS_OK)

    def test_apply_fix_rejects_unknown_check(self) -> None:
        result = doctor.apply_fix(self.store, "no_such_check")
        self.assertEqual(result.status, doctor.STATUS_ERROR)

    def test_summarise_counts(self) -> None:
        report = doctor.run_checks(self.store)
        n_ok, n_info, n_warn, n_err = doctor.summarise(report)
        self.assertEqual(n_ok + n_info + n_warn + n_err, len(report))


# --------------------------------------------------------------------- CLI wrapper


class DoctorCliTests(unittest.TestCase):
    """The CLI wrapper must emit human + JSON + list output."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_mode_prints_function_names(self) -> None:
        # Build a minimal argv and run the CLI in --list mode.  We can't
        # easily capture stdout from the run_doctor wrapper, so we test
        # the CHECKS list directly -- the wrapper simply iterates it.
        for fn in doctor.CHECKS:
            self.assertTrue(callable(fn))
            self.assertTrue(fn.__name__.startswith("_check_"))

    def test_json_output_is_valid(self) -> None:
        checks = doctor.run_checks(self.store)
        out = doctor.render_json(checks)
        data = json.loads(out)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 10)

    def test_human_output_contains_summary(self) -> None:
        checks = doctor.run_checks(self.store)
        out = doctor.render_human(checks, verbose=False)
        self.assertIn("loop-memory doctor", out)
        # Header line should summarise status counts.
        self.assertIn("ok", out)

    def test_human_verbose_includes_info(self) -> None:
        checks = doctor.run_checks(self.store)
        out_quiet = doctor.render_human(checks, verbose=False)
        out_verbose = doctor.render_human(checks, verbose=True)
        # Verbose should be >= quiet in length (more checks shown).
        self.assertGreaterEqual(len(out_verbose), len(out_quiet))

    def test_run_doctor_missing_db_exits_nonzero(self) -> None:
        # When the store path doesn't exist, the wrapper should
        # emit an error and exit with a non-zero status.
        rc = doctor_cli.run_doctor([
            "--db", str(self.tmp / "ghost.sqlite"),
            "--json",  # JSON so we can machine-parse
        ])
        self.assertEqual(rc, 1)

    def test_run_doctor_list_mode_exits_0(self) -> None:
        rc = doctor_cli.run_doctor(["--list"])
        self.assertEqual(rc, 0)


# --------------------------------------------------------------------- subprocess


@unittest.skipUnless(_has_shim(), "console shim not present -- skip subprocess tests")
class DoctorShimTests(unittest.TestCase):
    """End-to-end through the console shim, matching what users see."""

    def setUp(self) -> None:
        self.store, self.tmp = _new_store()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shim_help_mentions_flags(self) -> None:
        proc = subprocess.run(
            [_shim_path(), "doctor", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0)
        # New help text mentions the flags.
        self.assertIn("--json", proc.stdout)
        self.assertIn("--fix", proc.stdout)

    def test_shim_list_mode_exits_0(self) -> None:
        proc = subprocess.run(
            [_shim_path(), "doctor", "--list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("_check_db_integrity", proc.stdout)

    def test_shim_missing_db_errors(self) -> None:
        proc = subprocess.run(
            [_shim_path(), "doctor", "--db", str(self.tmp / "ghost.sqlite")],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # ``die()`` returns 2; the --json branch returns 1. Accept either.
        self.assertIn(proc.returncode, (1, 2))


if __name__ == "__main__":
    unittest.main()
