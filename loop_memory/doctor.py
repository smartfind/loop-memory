"""``loop-memory doctor`` — auto-diagnostic check library.

Audit 2026-10-12.  Pattern adopted from:

* ``cortexkit/magic-context`` ``npx @cortexkit/magic-context
  doctor`` (MIT, 2026-10-12) — auto-detect harness conflicts,
  verify plugin registration, check database integrity, fix
  what it can.
* ``PerryLink/dsh-memento`` ``dsh-plugin-doctor`` (Apache-2.0,
  2026-10-12) — CI-badge pattern for the DSH plugin ecosystem.

The *pattern* (run a small checklist of common-store problems
and report severity) is not copyrightable; only the code is,
and we re-implement from scratch against our own ``MemoryStore``
API.  No code copied.

Each check returns a :class:`DoctorCheck` with:

* ``name`` — stable identifier (used by ``--json`` and the
  doctor test suite).
* ``status`` — one of ``"ok"`` / ``"info"`` / ``"warn"`` /
  ``"error"``.
* ``message`` — human-readable one-liner.
* ``fix_hint`` — optional human-actionable suggestion.
* ``auto_fixable`` — ``True`` iff the CLI ``--fix`` flag may
  call :func:`MemoryStore.apply_doctor_fix` with the same
  ``name`` to repair the problem.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from importlib import metadata as _importlib_metadata
from pathlib import Path
from typing import Any, Callable, Iterable

# Status levels.  ``error`` exits the CLI non-zero; ``warn`` is
# reported but does not fail; ``info`` is reported only with
# ``--verbose``; ``ok`` is reported but not actionable.
STATUS_OK = "ok"
STATUS_INFO = "info"
STATUS_WARN = "warn"
STATUS_ERROR = "error"

# The 12 audit-trigger names installed by
# ``MemoryStore._install_audit_triggers()`` (audit 2026-09-27).
_AUDIT_TRIGGER_NAMES = tuple(
    f"trg_{table}_{op}"
    for table in ("memories", "wiki_pages", "relations", "consolidation_runs")
    for op in ("ai", "au", "ad")
)
# (12 names)


@dataclass
class DoctorCheck:
    """A single diagnostic result.

    ``auto_fixable=True`` means the CLI ``--fix`` flag may call
    :func:`MemoryStore.apply_doctor_fix` with this same ``name``
    to repair the problem.  The CLI prints a one-line summary for
    every check (ok / info / warn / error) and a verbose-mode
    message for ``info``-level checks.
    """
    name: str
    status: str
    message: str
    fix_hint: str | None = None
    auto_fixable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


def _installed_version() -> str | None:
    """Best-effort read of the installed loop-memory distribution
    version.  Returns ``None`` if the distribution metadata is
    missing (e.g. running from a source checkout without an
    editable install)."""
    try:
        return _importlib_metadata.version("loop-memory")
    except _importlib_metadata.PackageNotFoundError:
        return None


def _collect_installed_triggers(conn: sqlite3.Connection) -> list[str]:
    """List the audit-trigger names currently installed in
    ``conn``.  Returns ``[]`` if the schema is so old that
    ``sqlite_master`` is empty."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'trg_%' "
            "ORDER BY name"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [r["name"] if hasattr(r, "keys") else r[0] for r in rows]


def _expected_schema_version(store: Any) -> str | None:
    """Read the ``SCHEMA_VERSION`` class attribute off the
    ``MemoryStore`` class without importing it twice (the
    caller already imported it).  Falls back to the value in
    the DB's ``schema_meta`` table."""
    return getattr(store, "SCHEMA_VERSION", None)


def _read_schema_version_db(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version' LIMIT 1"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    if hasattr(row, "keys"):
        return str(row["value"])
    return str(row[0])


def _safe_count(conn: sqlite3.Connection, sql: str) -> int | None:
    """Run ``SELECT COUNT(*) FROM <table>`` and return the int.
    Returns ``None`` when the table is missing (returns the
    error ``no such table``)."""
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return 0
    if hasattr(row, "keys"):
        return int(row[0])
    return int(row[0])


def _check_db_exists(store: Any) -> DoctorCheck:
    path = Path(str(getattr(store, "path", "")))
    if not str(path):
        return DoctorCheck(
            name="db_exists",
            status=STATUS_ERROR,
            message="store has no path attribute; cannot run further checks",
            fix_hint="re-create the store with MemoryStore(<path>)",
        )
    if not path.exists():
        return DoctorCheck(
            name="db_exists",
            status=STATUS_ERROR,
            message=f"SQLite file not found: {path}",
            fix_hint="create the store with `loop-memory init` or `MemoryStore(path)`",
        )
    if not path.is_file():
        return DoctorCheck(
            name="db_exists",
            status=STATUS_ERROR,
            message=f"path exists but is not a regular file: {path}",
        )
    return DoctorCheck(
        name="db_exists",
        status=STATUS_OK,
        message=f"SQLite file present ({path})",
    )


def _check_db_integrity(store: Any) -> DoctorCheck:
    """``PRAGMA integrity_check`` — should return a single row
    with value ``ok`` for a healthy database.  Anything else is
    a corruption signal."""
    try:
        # NB: chained ``store._conn().__enter__()`` immediately triggers the
        # contextmanager's ``finally`` under Python 3.14, closing the conn
        # before ``execute`` runs.  Always use ``with`` instead.
        with store._conn() as c:  # type: ignore[attr-defined]
            rows = c.execute("PRAGMA integrity_check").fetchall()
    except Exception as e:  # pragma: no cover — defensive
        return DoctorCheck(
            name="db_integrity",
            status=STATUS_ERROR,
            message=f"could not run PRAGMA integrity_check: {e}",
        )
    if not rows:
        return DoctorCheck(
            name="db_integrity",
            status=STATUS_WARN,
            message="PRAGMA integrity_check returned no rows",
        )
    result = rows[0][0] if hasattr(rows[0], "__getitem__") and not hasattr(rows[0], "keys") else rows[0]["integrity_check"]
    if str(result).lower() == "ok":
        return DoctorCheck(
            name="db_integrity",
            status=STATUS_OK,
            message="PRAGMA integrity_check = ok",
        )
    return DoctorCheck(
        name="db_integrity",
        status=STATUS_ERROR,
        message=f"PRAGMA integrity_check = {result!r}",
        fix_hint="restore from a recent snapshot via `loop-memory restore <file>`",
    )


def _check_schema_version(store: Any) -> DoctorCheck:
    expected = _expected_schema_version(store)
    if expected is None:
        return DoctorCheck(
            name="schema_version",
            status=STATUS_WARN,
            message="MemoryStore has no SCHEMA_VERSION class attribute",
        )
    with store._conn() as c:  # type: ignore[attr-defined]
        actual = _read_schema_version_db(c)
    if actual is None:
        return DoctorCheck(
            name="schema_version",
            status=STATUS_INFO,
            message=(
                f"no schema_meta row yet (expected {expected!r}); "
                "the store is uninitialised — opening it once will create the schema"
            ),
            fix_hint="open the store with `MemoryStore(path)` to run schema_meta upsert",
        )
    if str(actual) != str(expected):
        return DoctorCheck(
            name="schema_version",
            status=STATUS_WARN,
            message=(
                f"DB schema_meta has version {actual!r} but the running code "
                f"expects {expected!r}"
            ),
            fix_hint=(
                "upgrade the loop-memory wheel so the running code matches the "
                "DB schema, or restore a snapshot of an older store"
            ),
            details={"expected": expected, "actual": actual},
        )
    return DoctorCheck(
        name="schema_version",
        status=STATUS_OK,
        message=f"schema_meta version matches running code ({expected!r})",
        details={"version": expected},
    )


def _check_audit_triggers(store: Any) -> DoctorCheck:
    """All 12 audit triggers (audit 2026-09-27) must be
    installed so a raw ``sqlite3`` writer cannot bypass
    ``audit_log``."""
    with store._conn() as c:  # type: ignore[attr-defined]
        installed = set(_collect_installed_triggers(c))
    missing = [n for n in _AUDIT_TRIGGER_NAMES if n not in installed]
    if not missing:
        return DoctorCheck(
            name="audit_triggers",
            status=STATUS_OK,
            message=f"all {len(_AUDIT_TRIGGER_NAMES)} audit triggers installed",
            details={"installed": sorted(installed)},
        )
    return DoctorCheck(
        name="audit_triggers",
        status=STATUS_ERROR,
        message=(
            f"missing {len(missing)}/{len(_AUDIT_TRIGGER_NAMES)} audit triggers: "
            f"{', '.join(missing)}"
        ),
        fix_hint=(
            "open the store once (re-running the install path) so "
            "MemoryStore._install_audit_triggers() can re-install them"
        ),
        auto_fixable=True,
        details={"missing": missing},
    )


def _check_audit_log_writable(store: Any) -> DoctorCheck:
    """A no-op INSERT into ``audit_log`` must succeed and be
    reversible in the same transaction.  A failure here is
    almost always a disk-full / read-only-mount / permission
    problem."""
    actor = "loop-memory doctor"
    try:
        with store._conn() as c:  # type: ignore[attr-defined]
            cur = c.execute(
                "INSERT INTO audit_log (at, actor, op, entity, entity_id, meta) "
                "VALUES (strftime('%s','now'), ?, 'PROBE', 'doctor', NULL, "
                "        json_object('source', 'doctor'))",
                (actor,),
            )
            c.execute("DELETE FROM audit_log WHERE rowid=?", (cur.lastrowid,))
    except Exception as e:  # pragma: no cover — defensive
        return DoctorCheck(
            name="audit_log_writable",
            status=STATUS_ERROR,
            message=f"audit_log probe INSERT failed: {e}",
            fix_hint="check disk space, mount mode, and DB file permissions",
        )
    return DoctorCheck(
        name="audit_log_writable",
        status=STATUS_OK,
        message="audit_log probe INSERT + DELETE succeeded",
    )


def _check_audit_log_size(store: Any) -> DoctorCheck:
    """A store where the audit_log has ballooned to 100× the
    combined memory+wiki row count probably hasn't been pruned
    since the user started writing.  Warn so they can prune."""
    with store._conn() as c:  # type: ignore[attr-defined]
        n_audit = _safe_count(c, "SELECT COUNT(*) FROM audit_log") or 0
        n_mem = _safe_count(c, "SELECT COUNT(*) FROM memories") or 0
        n_wiki = _safe_count(c, "SELECT COUNT(*) FROM wiki_pages") or 0
    baseline = max(1, n_mem + n_wiki)
    ratio = n_audit / baseline
    details = {
        "audit_log": n_audit,
        "memories": n_mem,
        "wiki_pages": n_wiki,
        "ratio": round(ratio, 2),
    }
    if ratio >= 100:
        return DoctorCheck(
            name="audit_log_size",
            status=STATUS_WARN,
            message=(
                f"audit_log is {n_audit:,} rows for {n_mem + n_wiki:,} memories+wiki "
                f"(ratio {ratio:.0f}×); consider pruning the older rows"
            ),
            fix_hint=(
                "DELETE FROM audit_log WHERE at < strftime('%s','now','-90 day') "
                "to keep the last 90 days (run inside a backup first)"
            ),
            details=details,
        )
    if ratio >= 20:
        return DoctorCheck(
            name="audit_log_size",
            status=STATUS_INFO,
            message=(
                f"audit_log is {n_audit:,} rows ({ratio:.0f}× the memory+wiki count); "
                "still under the warn threshold but growing"
            ),
            details=details,
        )
    return DoctorCheck(
        name="audit_log_size",
        status=STATUS_OK,
        message=(
            f"audit_log size is healthy ({n_audit:,} rows for {n_mem + n_wiki:,} "
            f"memories+wiki, ratio {ratio:.1f}×)"
        ),
        details=details,
    )


def _check_hooks_installed(store: Any) -> DoctorCheck:
    """At least one known agent (codex/claude/hermes/openclaw)
    should be registered with ``hooks_installed=1`` so the
    auto-capture pipeline is alive.  Empty store + no hooks is
    a normal "first install" state and is reported as info,
    not warn."""
    with store._conn() as c:  # type: ignore[attr-defined]
        try:
            rows = c.execute(
                "SELECT name, hooks_installed FROM agents ORDER BY name"
            ).fetchall()
        except sqlite3.DatabaseError:
            return DoctorCheck(
                name="hooks_installed",
                status=STATUS_INFO,
                message="no agents table yet (store not opened yet?)",
            )
    hooked = [r["name"] for r in rows if int(r["hooks_installed"] or 0) == 1]
    if not rows:
        return DoctorCheck(
            name="hooks_installed",
            status=STATUS_INFO,
            message="no agents registered yet (run `loop-memory init --agent <name>`)",
        )
    if not hooked:
        names = [r["name"] for r in rows]
        return DoctorCheck(
            name="hooks_installed",
            status=STATUS_WARN,
            message=(
                f"agents {names!r} are registered but none have hooks_installed=1; "
                "auto-capture is not running for any of them"
            ),
            fix_hint="run `loop-memory install-hooks` to install the hooks for the agent you want",
        )
    return DoctorCheck(
        name="hooks_installed",
        status=STATUS_OK,
        message=f"hooks_installed=1 for: {', '.join(hooked)}",
        details={"hooked": hooked},
    )


def _check_version_consistent(store: Any) -> DoctorCheck:
    """``loop_memory.__version__`` (sourced from the installed
    metadata by the 0.4.5 fix) should match the distribution
    metadata so the running process and the wheel agree."""
    runtime = getattr(store, "_dist_version", None)
    if runtime is None:
        # The store doesn't carry the version directly; fall back
        # to importing loop_memory.
        try:
            from loop_memory import __version__ as runtime_v  # type: ignore
            runtime = runtime_v
        except Exception:
            return DoctorCheck(
                name="version_consistent",
                status=STATUS_INFO,
                message="could not determine the running loop_memory version",
            )
    installed = _installed_version()
    if installed is None:
        return DoctorCheck(
            name="version_consistent",
            status=STATUS_INFO,
            message=(
                f"runtime reports loop_memory {runtime!r} but the distribution metadata "
                "is unreadable; likely running from a source checkout"
            ),
            details={"runtime": runtime, "installed": None},
        )
    if str(runtime) != str(installed):
        return DoctorCheck(
            name="version_consistent",
            status=STATUS_WARN,
            message=(
                f"runtime loop_memory.__version__ = {runtime!r} but installed "
                f"distribution metadata says {installed!r}"
            ),
            fix_hint=(
                "re-install the package (`pip install -e .` in the repo, or "
                "`pip install --upgrade loop-memory`) so the editable install "
                "metadata matches the running code"
            ),
            details={"runtime": runtime, "installed": installed},
        )
    return DoctorCheck(
        name="version_consistent",
        status=STATUS_OK,
        message=f"runtime and distribution metadata agree on {runtime!r}",
        details={"version": runtime},
    )


def _check_recall_paths_works(store: Any) -> DoctorCheck:
    """``recall_paths()`` should return the expected shape
    (memories / wiki / entities / tokens keys) on a benign
    query.  A failure here is almost always a schema or
    tokeniser regression."""
    try:
        r = store.recall_paths("loop memory", limit=3)  # type: ignore[attr-defined]
    except Exception as e:
        return DoctorCheck(
            name="recall_paths_works",
            status=STATUS_ERROR,
            message=f"recall_paths('loop memory') raised: {e}",
            fix_hint=(
                "this is a code regression; please file an issue with the "
                "traceback and your loop-memory version"
            ),
        )
    expected = {"memories", "wiki", "entities", "tokens"}
    if not expected.issubset(r.keys()):
        return DoctorCheck(
            name="recall_paths_works",
            status=STATUS_ERROR,
            message=(
                f"recall_paths() returned keys {sorted(r.keys())}; "
                f"missing {sorted(expected - set(r.keys()))}"
            ),
        )
    if not isinstance(r["memories"], list) or not isinstance(r["wiki"], list):
        return DoctorCheck(
            name="recall_paths_works",
            status=STATUS_ERROR,
            message="recall_paths() returned non-list memories/wiki",
        )
    return DoctorCheck(
        name="recall_paths_works",
        status=STATUS_OK,
        message=(
            f"recall_paths() returned shape ok "
            f"({len(r['memories'])} mem, {len(r['wiki'])} wiki, {len(r['entities'])} ent)"
        ),
        details={
            "memories": len(r["memories"]),
            "wiki": len(r["wiki"]),
            "entities": len(r["entities"]),
        },
    )


def _check_wiki_no_orphans(store: Any) -> DoctorCheck:
    """Defensive: every wiki row that ``relations`` references
    should still exist.  ``relations`` is not declared with a
    foreign key (audit 2026-08-30, lookup-by-slug pattern), so
    a stray DELETE on a wiki row could leave dangling
    references.  Report any as warn."""
    with store._conn() as c:  # type: ignore[attr-defined]
        try:
            rows = c.execute(
                "SELECT DISTINCT r.dst FROM relations r "
                "WHERE r.dst_kind='wiki' "
                "  AND NOT EXISTS (SELECT 1 FROM wiki_pages w WHERE w.slug=r.dst)"
            ).fetchall()
        except sqlite3.DatabaseError:
            return DoctorCheck(
                name="wiki_no_orphans",
                status=STATUS_INFO,
                message="no relations table yet (store not opened yet?)",
            )
    if not rows:
        return DoctorCheck(
            name="wiki_no_orphans",
            status=STATUS_OK,
            message="no relations rows point at a missing wiki slug",
        )
    slugs = [r["dst"] for r in rows]
    return DoctorCheck(
        name="wiki_no_orphans",
        status=STATUS_WARN,
        message=(
            f"{len(slugs)} relations row(s) reference a missing wiki slug: "
            f"{slugs[:5]}{'…' if len(slugs) > 5 else ''}"
        ),
        fix_hint=(
            "either re-create the missing wiki page or DELETE the dangling "
            "relation rows by hand after a backup"
        ),
        details={"orphans": slugs},
    )


# The full check library.  Each entry is a callable that takes
# the live ``MemoryStore`` and returns a ``DoctorCheck``.
CHECKS: tuple[Callable[[Any], DoctorCheck], ...] = (
    _check_db_exists,
    _check_db_integrity,
    _check_schema_version,
    _check_audit_triggers,
    _check_audit_log_writable,
    _check_audit_log_size,
    _check_hooks_installed,
    _check_version_consistent,
    _check_wiki_no_orphans,
    _check_recall_paths_works,
)


def run_checks(store: Any) -> list[DoctorCheck]:
    """Run every check in :data:`CHECKS` against ``store`` and
    return the result list.  Each check is isolated; one
    failure does not stop the others.  Use
    :func:`summarise` to render the result for a CLI."""
    out: list[DoctorCheck] = []
    for c in CHECKS:
        try:
            out.append(c(store))
        except Exception as e:  # pragma: no cover — defensive
            out.append(DoctorCheck(
                name=getattr(c, "__name__", "unknown"),
                status=STATUS_ERROR,
                message=f"check raised an unhandled exception: {e}",
            ))
    return out


def apply_fix(store: Any, name: str) -> DoctorCheck:
    """Attempt to auto-repair the check identified by ``name``.

    Only checks that report ``auto_fixable=True`` may be fixed
    here.  The CLI ``--fix`` flag calls this once per failing
    check until the report is clean or the budget is exhausted.
    """
    if name == "audit_triggers":
        try:
            with store._conn() as c:  # type: ignore[attr-defined]
                store._install_audit_triggers(c)  # type: ignore[attr-defined]
        except Exception as e:
            return DoctorCheck(
                name=name,
                status=STATUS_ERROR,
                message=f"auto-fix failed: {e}",
            )
        return DoctorCheck(
            name=name,
            status=STATUS_OK,
            message="auto-fix re-installed the missing audit triggers",
        )
    return DoctorCheck(
        name=name,
        status=STATUS_ERROR,
        message=(
            f"no auto-fix available for {name!r}; follow the fix_hint above "
            "or run `loop-memory --help` for manual steps"
        ),
    )


def summarise(checks: Iterable[DoctorCheck], *, verbose: bool = False) -> tuple[int, int, int, int]:
    """Count ``(ok, info, warn, error)`` for a quick "is the
    store healthy?" glance."""
    n_ok = n_info = n_warn = n_err = 0
    for c in checks:
        if c.status == STATUS_OK:
            n_ok += 1
        elif c.status == STATUS_INFO:
            n_info += 1
        elif c.status == STATUS_WARN:
            n_warn += 1
        elif c.status == STATUS_ERROR:
            n_err += 1
    return n_ok, n_info, n_warn, n_err


def exit_code(checks: Iterable[DoctorCheck]) -> int:
    """Map a check list to a CLI exit code: 0 if no errors, 1
    otherwise.  Warnings and infos do not fail the exit code so
    a noisy store does not break CI scripts that just want to
    confirm the store is reachable."""
    for c in checks:
        if c.status == STATUS_ERROR:
            return 1
    return 0


def render_human(checks: list[DoctorCheck], *, verbose: bool = False) -> str:
    """Format the check list for a human reading the terminal.
    The format is:

    ```
    loop-memory doctor — <N> ok, <N> info, <N> warn, <N> error
      [OK]      db_exists             SQLite file present (/…/loop_memory.db)
      [ERROR]   audit_triggers        missing 2/12 audit triggers: trg_…
      [WARN]    hooks_installed       no agents registered yet
      …
    ```
    """
    n_ok, n_info, n_warn, n_err = summarise(checks)
    lines: list[str] = []
    head = (
        f"loop-memory doctor — {n_ok} ok, {n_info} info, "
        f"{n_warn} warn, {n_err} error"
    )
    lines.append(head)
    label = {
        STATUS_OK: "[OK]    ",
        STATUS_INFO: "[INFO]  ",
        STATUS_WARN: "[WARN]  ",
        STATUS_ERROR: "[ERROR] ",
    }
    for c in checks:
        if c.status == STATUS_INFO and not verbose:
            continue
        lines.append(
            f"  {label[c.status]}{c.name:<24s}  {c.message}"
        )
        if c.fix_hint and c.status in (STATUS_WARN, STATUS_ERROR):
            lines.append(
                f"                                  ↳ hint: {c.fix_hint}"
            )
    return "\n".join(lines) + "\n"


def render_json(checks: list[DoctorCheck]) -> str:
    """Format the check list as JSON for scripting.  Stable
    shape so a CI script can grep on ``status`` /
    ``name``."""
    return json.dumps(
        [asdict(c) for c in checks],
        indent=2,
        sort_keys=True,
    ) + "\n"
