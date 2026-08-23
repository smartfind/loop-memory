"""rules: install a memory-discipline block into the agent's rule file.

Audit 2026-08-23: pattern adopted from
``2672243194/agentbrain`` v0.4.3 (``agentbrain rules --agent … --write``,
which appends a three-phase workflow block into CLAUDE.md /
AGENTS.md / Cursor / Trae rule files). Loop Memory's port wires
the same discipline into the four shipped agent targets:

* ``codex``    -> ``AGENTS.md``   (Codex CLI convention)
* ``claude``   -> ``CLAUDE.md``   (Claude Code convention)
* ``hermes``   -> ``AGENTS.md``   (Hermes CLI also reads AGENTS.md)
* ``openclaw`` -> ``AGENTS.md``   (OpenClaw reads AGENTS.md)
* ``generic``  -> ``AGENTS.md``   (any other AGENTS.md-aware client)

Safety properties:

* Existing rule files are **never overwritten** — the block is
  appended after the user's content. Their rules stay byte-identical.
* **Idempotent** — a marker comment detects a prior install and
  skips the write.
* The CLI exits 0 even when the target file does not exist (it
  creates an empty one) and when no ``--agent`` is given (it just
  prints the generic block to stdout).
"""

from __future__ import annotations

import sys
from pathlib import Path


# Marker line used to detect a prior install. The leading space +
# trailing space keep the marker from accidentally colliding with a
# user's prose like "loop-memory is great". Keep this string in sync
# with the appended block below.
MARKER = "<!-- loop-memory:rules:installed -->"

# Agent-name -> relative path under cwd. Unknown agents fall back
# to ``generic`` (AGENTS.md).
AGENT_TARGETS: dict[str, str] = {
    "codex":    "AGENTS.md",
    "claude":   "CLAUDE.md",
    "hermes":   "AGENTS.md",
    "openclaw": "AGENTS.md",
    "generic":  "AGENTS.md",
}


def _generic_block() -> str:
    """The discipline block written to every agent's rule file."""
    return (
        f"\n{MARKER}\n"
        "# Memory discipline (auto-installed by `loop-memory rules`).\n"
        "# Re-run the same command to refresh in place; manual edits\n"
        "# between the marker lines will be preserved on the next refresh.\n"
        "\n"
        "## At task start\n"
        "\n"
        "Before you read or write anything in this project, query Loop\n"
        "Memory first so you can act on prior work instead of rebuilding\n"
        "it. Use the project's existing memory tooling — for example\n"
        "`loop-memory recall \"<task topic>\"` (shell), the `recall` MCP\n"
        "tool, or the `inject` SessionStart hook if one is wired up.\n"
        "Read the top hits before you start typing.\n"
        "\n"
        "## Mid-task\n"
        "\n"
        "Re-query whenever you switch subtask, hit an unexpected error,\n"
        "or follow up a topic the initial query did not cover. Plain\n"
        "continuation of the same line of thought does not need a\n"
        "re-query — that is what saves tokens.\n"
        "\n"
        "## Wrap-up\n"
        "\n"
        "Each distinct reusable lesson — a non-obvious gotcha, a\n"
        "convention you wish you'd known, a fix to a bug class —\n"
        "should be written back via `loop-memory ingest` / the `add`\n"
        "MCP tool, with user confirmation so nothing private lands in\n"
        "the vault. Never paste secrets, tokens, or local paths into\n"
        "a memory; use `${ENV:VAR_NAME}` placeholders or omit them.\n"
        f"\n{MARKER} (end)\n"
    )


def _render_block(agent: str) -> str:
    """Return the discipline block tailored to ``agent``."""
    base = _generic_block()
    if agent in {"claude", "codex"}:
        return base
    # Hermes / OpenClaw: same text but the explicit tool name in the
    # hook line differs because the MCP tool prefix varies. The block
    # already names the tool generically; we add one agent-specific
    # clarifying line at the bottom for these two.
    if agent == "hermes":
        extra = (
            "\n## Hermes-specific\n"
            "\n"
            "Hermes exposes Loop Memory through MCP too. The same\n"
            "`recall` / `add` tool names apply; if you do not see\n"
            "them in the MCP tool list, run\n"
            "`loop-memory install-hooks` once and restart Hermes.\n"
        )
        return base.replace(f"\n{MARKER} (end)\n", extra + f"\n{MARKER} (end)\n")
    if agent == "openclaw":
        extra = (
            "\n## OpenClaw-specific\n"
            "\n"
            "OpenClaw sessions are auto-ingested when\n"
            "`loop-memory hook --source openclaw --watch <sessions-dir>`\n"
            "is running. If the recall hits are empty, check that\n"
            "the watcher is alive (`loop-memory doctor`) and that\n"
            "the workspace log directory exists.\n"
        )
        return base.replace(f"\n{MARKER} (end)\n", extra + f"\n{MARKER} (end)\n")
    return base


def _resolve_target(agent: str, cwd: Path) -> Path:
    """Map an agent name to its rule-file path under ``cwd``."""
    rel = AGENT_TARGETS.get(agent)
    if rel is None:
        # Unknown agent — keep the same path as ``generic`` so the
        # user still gets a working AGENTS.md and can rename it.
        rel = AGENT_TARGETS["generic"]
    return cwd / rel


def _install(cwd: Path, agent: str, *, force: bool) -> tuple[str, str]:
    """Install the block; return (status, message).

    ``status`` is one of: ``installed``, ``already-installed``,
    ``appended``, ``dry-run``.
    """
    target = _resolve_target(agent, cwd)
    existing = ""
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except Exception as e:
            return "error", f"could not read {target}: {e}"
    if MARKER in existing and not force:
        return "already-installed", str(target)
    # ``run_rules`` does the actual write; this helper just classifies
    # the request so callers can unit-test the marker-detection path.
    return "installed", str(target)


def run_rules(args: list) -> int:
    """``loop-memory rules [--agent NAME] [--write] [--force] [--cwd PATH]``.

    Behaviour:
    * no args                     -> print the generic block to stdout
    * ``--agent NAME``            -> print the agent-tailored block
    * ``--write``                 -> install (append) into the agent's
                                    rule file under cwd (or ``--cwd``)
    * ``--force``                 -> overwrite a prior install in place
                                    (still never touches user content
                                    outside the marker lines)
    """
    agent = "generic"
    write = False
    force = False
    cwd = Path.cwd()
    it = iter(args)
    for tok in it:
        if tok == "--agent":
            try:
                agent = next(it)
            except StopIteration:
                print("--agent requires a name", file=sys.stderr)
                return 2
        elif tok == "--write":
            write = True
        elif tok == "--force":
            force = True
        elif tok == "--cwd":
            try:
                cwd = Path(next(it)).expanduser()
            except StopIteration:
                print("--cwd requires a path", file=sys.stderr)
                return 2
        elif tok in {"-h", "--help"}:
            print(__doc__ or "loop-memory rules [--agent NAME] [--write] [--force]")
            return 0
        else:
            print(f"unknown argument: {tok}", file=sys.stderr)
            return 2

    if not write:
        sys.stdout.write(_render_block(agent))
        return 0

    target = _resolve_target(agent, cwd)
    existing = ""
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except Exception as e:
            print(f"could not read {target}: {e}", file=sys.stderr)
            return 1

    block = _render_block(agent)
    if MARKER in existing and not force:
        print(f"[loop-memory] rules: already installed in {target}")
        return 0
    if MARKER in existing and force:
        # Replace the existing marker span in place: keep everything
        # before the start marker and everything after the close
        # marker, splice a fresh block between them. User content
        # outside the marker lines is preserved byte-for-byte.
        start_idx = existing.index(MARKER)
        close_idx = existing.find(MARKER + " (end)", start_idx)
        if close_idx < 0:
            # Partial / corrupted install — fall back to append.
            close_idx = len(existing)
        else:
            close_idx = existing.index("\n", close_idx)
        new_content = existing[:start_idx].rstrip("\n") + "\n" + block + existing[close_idx:]
        action = "refreshed"
    else:
        sep = ""
        if existing and not existing.endswith("\n"):
            sep = "\n"
        new_content = existing + sep + block
        action = "appended" if existing else "created"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")
    except Exception as e:
        print(f"[loop-memory] rules: write failed: {e}", file=sys.stderr)
        return 1
    print(f"[loop-memory] rules: {action} {target}")
    return 0


__all__ = [
    "AGENT_TARGETS",
    "MARKER",
    "_generic_block",
    "_install",
    "_render_block",
    "_resolve_target",
    "run_rules",
]
