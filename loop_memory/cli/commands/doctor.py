"""``loop-memory doctor`` subcommand.

Audit 2026-10-12.  Wraps :mod:`loop_memory.doctor` and renders the
result for a human or as JSON.  See ``loop_memory/doctor.py`` for
the check library and the source attribution.

Usage examples::

    loop-memory doctor                       # human-readable report
    loop-memory doctor --json                # machine-readable report
    loop-memory doctor --verbose             # also show INFO-level checks
    loop-memory doctor --fix                 # auto-repair auto_fixable checks
    loop-memory doctor --db /path/to/store.db  # run against a specific DB
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .._common import die
from ... import doctor as _doctor
from ...storage.sqlite_store import MemoryStore


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loop-memory doctor",
        description=(
            "Run a small library of health checks against the loop-memory "
            "store.  Reports ok / info / warn / error per check and exits "
            "non-zero if any check is in 'error' state.  Source: cortexkit/"
            "magic-context doctor (MIT, 2026-10-12) + PerryLink/dsh-memento "
            "dsh-plugin-doctor (Apache-2.0, 2026-10-12) — pattern only."
        ),
    )
    p.add_argument(
        "--db",
        default=None,
        help="path to the SQLite file (default: $LOOP_MEMORY_DB or ~/.loop_memory/loop_memory.db)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the result as JSON for scripting",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="also show INFO-level checks in human output",
    )
    p.add_argument(
        "--fix",
        action="store_true",
        help="auto-repair any auto_fixable check that fails (idempotent; safe to re-run)",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="list every check the doctor would run and exit (no DB access)",
    )
    return p


def _resolve_db_path(arg: str | None) -> Path:
    """Pick the store path: ``--db`` wins, else ``$LOOP_MEMORY_DB``,
    else the default ``~/.loop_memory/loop_memory.db``."""
    if arg:
        return Path(arg).expanduser()
    import os
    env = os.environ.get("LOOP_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    from .._common import default_db_path
    return Path(default_db_path())


def run_doctor(args: list[str]) -> int:
    parser = _build_parser()
    parsed = parser.parse_args(args)

    if parsed.list:
        for fn in _doctor.CHECKS:
            print(fn.__name__)
        return 0

    db_path = _resolve_db_path(parsed.db)
    if not db_path.exists():
        msg = (
            f"loop-memory doctor: store file not found at {db_path}. "
            "Run `loop-memory init` first, or pass --db to point at an existing store."
        )
        if parsed.json:
            print(_doctor.render_json([
                _doctor.DoctorCheck(
                    name="db_exists",
                    status=_doctor.STATUS_ERROR,
                    message=msg,
                    fix_hint="run `loop-memory init` to create the store",
                ),
            ]))
            return 1
        return die(msg)

    # ``MemoryStore(<missing_path>)`` creates a fresh DB on open.
    # We want doctor to FAIL when the path is missing rather than
    # silently materialise an empty store, so the missing-path
    # check above is the contract.
    try:
        store = MemoryStore(str(db_path))
    except Exception as e:
        msg = f"loop-memory doctor: could not open store at {db_path}: {e}"
        if parsed.json:
            print(_doctor.render_json([
                _doctor.DoctorCheck(
                    name="db_exists",
                    status=_doctor.STATUS_ERROR,
                    message=msg,
                ),
            ]))
            return 1
        return die(msg)

    checks = _doctor.run_checks(store)

    if parsed.fix:
        # Apply auto-fixes in a single pass.  We re-run the checks
        # so the user sees the post-fix state in the same command.
        for c in checks:
            if c.status in (_doctor.STATUS_ERROR, _doctor.STATUS_WARN) and c.auto_fixable:
                post = _doctor.apply_fix(store, c.name)
                # Replace the failing check with the post-fix result
                # so the human / JSON output reflects reality.
                idx = checks.index(c)
                checks[idx] = post
        checks = _doctor.run_checks(store)

    if parsed.json:
        sys.stdout.write(_doctor.render_json(checks))
    else:
        sys.stdout.write(_doctor.render_human(checks, verbose=parsed.verbose))

    return _doctor.exit_code(checks)
