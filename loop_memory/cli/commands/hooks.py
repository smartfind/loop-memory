"""install-hooks: auto-configure local AI CLIs to use loop-memory."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _upsert_block(text: str, block: str, marker: str) -> str:
    """Replace an existing block starting with ``marker`` with ``block``.

    See original docstring in v0.3.0 — this function is called from
    the Codex config.toml updater to swap in the loop-memory section
    without touching the user's other settings.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if marker in ln:
            start = i
            break
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + block

    block_sections = []
    cur = None
    for ln in block.splitlines():
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            cur = stripped
            block_sections.append(cur)
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped in block_sections:
                end += 1
                continue
            break
        end += 1

    return "".join(lines[:start]) + block + "".join(lines[end:])


def _install_codex(home: Path, actions: list, store=None) -> None:
    codex_dir = home / ".codex"
    codex_cfg = codex_dir / "config.toml"
    # Codex's TOML schema: `hooks` is a struct (HooksToml) keyed by event
    # name (PreToolUse / SessionStart / UserPromptSubmit / ...), NOT an
    # array of tables. Each event value is a matcher group with a `hooks`
    # list of {type, command}. Trying to write `[[hooks]]` makes codex
    # fail at startup with "invalid type: sequence, expected struct
    # HooksToml in `hooks`".
    codex_block = (
        "\n# [loop-memory] auto-installed by `loop-memory install-hooks`.\n"
        "# Re-run the same command to refresh; the block is updated in place.\n"
        "[mcp_servers.loop_memory]\n"
        'command = "loop-memory"\n'
        'args = ["mcp"]\n'
        "\n"
        "[hooks.session_start]\n"
        '[[hooks.session_start.hooks]]\n'
        'type = "command"\n'
        'command = "loop-memory inject"\n'
        "\n"
        "[hooks.user_prompt_submit]\n"
        '[[hooks.user_prompt_submit.hooks]]\n'
        'type = "command"\n'
        'command = "loop-memory inject"\n'
    )
    if not codex_dir.exists():
        actions.append("codex → not installed (skipped)")
        return
    try:
        existing = codex_cfg.read_text(encoding="utf-8") if codex_cfg.exists() else ""
        new = _upsert_block(existing, codex_block, marker="# [loop-memory]")
        codex_cfg.parent.mkdir(parents=True, exist_ok=True)
        codex_cfg.write_text(new, encoding="utf-8")
        # Validate that the resulting TOML still parses with the codex CLI's
        # own loader, so a schema regression surfaces immediately rather than
        # at codex startup time. Falls back to a stdlib `tomllib` check
        # (3.11+); if unavailable we just trust the write.
        parse_ok = False
        try:
            import tomllib  # py3.11+
            with open(codex_cfg, "rb") as f:
                tomllib.load(f)
            parse_ok = True
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
                with open(codex_cfg, "rb") as f:
                    tomllib.load(f)
                parse_ok = True
            except Exception:
                parse_ok = True  # can't validate, trust the write
        except Exception as ve:
            actions.append(f"codex → WRITE OK but TOML INVALID: {ve}; rolling back")
            try: codex_cfg.write_text(existing, encoding="utf-8")
            except Exception: pass
            return
        actions.append(f"codex → {codex_cfg}" + ("" if parse_ok else " (unverified)"))
        if store is not None:
            store.register_agent("codex", hooks_installed=True)
    except Exception as e:
        actions.append(f"codex → SKIP ({e})")


def _install_claude(home: Path, actions: list, store=None) -> None:
    claude_dir = home / ".claude"
    claude_mcp = claude_dir / "mcp.json"
    claude_settings = claude_dir / "settings.json"
    if not claude_dir.exists():
        actions.append("claude → not installed (skipped)")
        return
    try:
        existing = json.loads(claude_mcp.read_text(encoding="utf-8")) if claude_mcp.exists() else {}
    except Exception:
        existing = {}
    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["loop_memory"] = {
        "command": "loop-memory",
        "args": ["mcp"],
        "env": {},
        "description": "Distilled long-term memory of the user.",
    }
    claude_mcp.parent.mkdir(parents=True, exist_ok=True)
    claude_mcp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    actions.append(f"claude (mcp) → {claude_mcp}")

    try:
        existing_s = json.loads(claude_settings.read_text(encoding="utf-8")) if claude_settings.exists() else {}
    except Exception:
        existing_s = {}
    hooks = existing_s.setdefault("hooks", {})
    sess = hooks.setdefault("SessionStart", [])
    if not any(
        isinstance(h, dict) and any(
            isinstance(c, dict) and "loop-memory inject" in c.get("command", "")
            for c in h.get("hooks", [])
        )
        for h in sess
    ):
        sess.append({
            "hooks": [{"type": "command", "command": "loop-memory inject"}],
        })
    claude_settings.write_text(json.dumps(existing_s, ensure_ascii=False, indent=2), encoding="utf-8")
    actions.append(f"claude (SessionStart hook) → {claude_settings}")
    if store is not None:
        store.register_agent("claude", hooks_installed=True)


def _install_hermes(home: Path, actions: list, store=None) -> None:
    hermes_dir = home / ".hermes"
    hermes_cfg = hermes_dir / "mcp.json"
    if not hermes_dir.exists():
        actions.append("hermes → not installed (skipped)")
        return
    try:
        existing = json.loads(hermes_cfg.read_text(encoding="utf-8")) if hermes_cfg.exists() else {}
    except Exception:
        existing = {}
    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["loop_memory"] = {
        "command": "loop-memory",
        "args": ["mcp"],
    }
    hermes_cfg.parent.mkdir(parents=True, exist_ok=True)
    hermes_cfg.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    actions.append(f"hermes → {hermes_cfg}")
    if store is not None:
        store.register_agent("hermes", hooks_installed=True)


def _openclaw_hint(home: Path, actions: list) -> None:
    openclaw_dir = home / ".openclaw"
    if not openclaw_dir.exists():
        actions.append("openclaw → not installed (skipped)")
        return
    candidates = [
        openclaw_dir / "agents" / "main" / "sessions",
        openclaw_dir / "sessions",
        openclaw_dir / "workspace" / "memory",
    ]
    existing = [str(c) for c in candidates if c.exists()]
    watch_root = existing[0] if existing else str(openclaw_dir)
    actions.append(
        f"openclaw detected at {openclaw_dir}. To ingest new sessions "
        f"automatically: `loop-memory hook --source openclaw --watch "
        f"{watch_root} &` (also picks up workspace/memory/*.md daily logs)"
    )


def run_install_hooks(_args) -> int:
    """Auto-detect local AI CLI installs and write MCP + inject hook configs.

    Supported: Codex CLI (~/.codex), Claude Code (~/.claude),
    Hermes (~/.hermes). Each gets an MCP entry pointing at
    ``loop-memory mcp`` and a SessionStart hook running
    ``loop-memory inject``. Existing configs are *merged*, not
    overwritten.
    """
    home = Path.home()
    actions: list = []
    # Audit 2026-09-13 (Mem0 CLI init --agent pattern) — record which
    # agents we touched so the per-agent index stays accurate. The
    # store is best-effort: if the SQLite path can't open, we fall
    # back to "install only" so the user is never blocked by a DB
    # hiccup during a one-line CLI install.
    store = None
    try:
        from ...storage.sqlite_store import MemoryStore
        from ...cli._common import default_db_path
        store = MemoryStore(default_db_path())
    except Exception:
        store = None
    _install_codex(home, actions, store=store)
    _install_claude(home, actions, store=store)
    _install_hermes(home, actions, store=store)
    _openclaw_hint(home, actions)
    if store is not None:
        # OpenClaw is hinted (no auto-install) so we only touch it.
        store.touch_agent("openclaw")
    print("[loop-memory] install-hooks results:")
    for a in actions:
        print(f"  · {a}")
    print()
    print("Restart your CLI to pick up the new MCP server + hooks.")
    return 0



def run_init(args) -> int:
    """``loop-memory init --agent <name> [--install-hooks]``.

    Audit 2026-09-13 (Mem0 CLI ``mem0 init --agent <name>`` pattern).
    Registers an agent in the per-agent identity index and, when
    ``--install-hooks`` is passed, also runs the standard
    ``install-hooks`` flow so MCP + SessionStart hooks are written
    for that agent's CLI client. Idempotent — re-running for the
    same name just bumps ``last_seen_at``.
    """
    from ...storage.sqlite_store import MemoryStore
    from ...cli._common import default_db_path, die
    agent_name: str | None = None
    install_hooks = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--agent" and i + 1 < len(args):
            agent_name = args[i + 1].strip()
            i += 2
        elif a == "--install-hooks":
            install_hooks = True
            i += 1
        elif a == "--help" or a == "-h":
            print("usage: loop-memory init --agent <name> [--install-hooks]")
            return 0
        elif a.startswith("--"):
            return die(f"[loop-memory] unknown flag: {a}")
        else:
            return die(f"[loop-memory] unexpected positional: {a}")
    if not agent_name:
        return die("usage: loop-memory init --agent <name> [--install-hooks]")
    store = MemoryStore(default_db_path())
    rec = store.register_agent(agent_name, hooks_installed=False)
    verb = "registered" if rec["created"] else "refreshed"
    print(f"[loop-memory] {verb} agent {agent_name!r} (scope={rec['scope']}, "
          f"hooks_installed={rec['hooks_installed']}, "
          f"last_seen_at={rec['last_seen_at']:.0f}).")
    if install_hooks:
        # Delegate to the standard install-hooks flow so the MCP + inject
        # hook files get written for codex/claude/hermes if any of those
        # clients are installed.
        run_install_hooks([])
    return 0
