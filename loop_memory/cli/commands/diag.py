"""Diagnostic commands: ``loop-memory doctor`` and ``loop-memory status``.

Designed to be the first thing a new user runs after ``pip install``.
Prints a single screen with green/red dots for every subsystem so
the user can see at a glance what's wired and what's missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .._common import DEFAULT_DB


_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def _dot(ok: bool | None, msg: str) -> str:
    """Return a single coloured status line. ``None`` is a soft warning."""
    if ok is True:
        icon = f"{_GREEN}●{_RESET}"
    elif ok is False:
        icon = f"{_RED}●{_RESET}"
    else:
        icon = f"{_YELLOW}●{_RESET}"
    return f"  {icon} {msg}"


def _has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def _detect_clients() -> dict[str, dict]:
    """Discover local AI CLIs the user has installed."""
    home = Path.home()
    out: dict[str, dict] = {}
    out["codex"] = {
        "installed": (home / ".codex").exists(),
        "config": str(home / ".codex" / "config.toml"),
        "mcp_configured": False,
    }
    codex_cfg = home / ".codex" / "config.toml"
    if codex_cfg.exists():
        try:
            text = codex_cfg.read_text(encoding="utf-8", errors="ignore")
            out["codex"]["mcp_configured"] = "loop-memory" in text
        except Exception:
            pass

    out["claude"] = {
        "installed": (home / ".claude").exists(),
        "config": str(home / ".claude" / "settings.json"),
        "mcp_configured": False,
    }
    claude_mcp = home / ".claude" / "mcp.json"
    if claude_mcp.exists():
        try:
            data = json.loads(claude_mcp.read_text(encoding="utf-8"))
            out["claude"]["mcp_configured"] = "loop_memory" in (
                data.get("mcpServers") or {}
            )
        except Exception:
            pass

    out["hermes"] = {
        "installed": (home / ".hermes").exists(),
        "config": str(home / ".hermes" / "mcp.json"),
        "mcp_configured": False,
    }
    hermes_cfg = home / ".hermes" / "mcp.json"
    if hermes_cfg.exists():
        try:
            data = json.loads(hermes_cfg.read_text(encoding="utf-8"))
            out["hermes"]["mcp_configured"] = "loop_memory" in (
                data.get("mcpServers") or {}
            )
        except Exception:
            pass

    openclaw_dir = home / ".openclaw"
    candidates = [
        openclaw_dir / "agents" / "main" / "sessions",
        openclaw_dir / "sessions",
        openclaw_dir / "workspace" / "memory",
    ]
    existing = [c for c in candidates if c.exists()]
    out["openclaw"] = {
        "installed": openclaw_dir.exists(),
        "watch_paths": [str(c) for c in existing],
        "watcher_running": False,
    }
    return out


def _detect_watcher() -> bool:
    """Return True if any ``loop-memory hook`` watcher is running.

    Probes both ``pgrep`` (foreground processes) and ``launchctl list``
    (daemonised watchers installed by ``loop-memory openclaw-setup``)
    so we don't miss a watcher that was started by launchd.
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", "loop_memory.cli.main hook"],
            text=True, timeout=2,
        )
        if "loop_memory.cli.main hook" in (out or ""):
            return True
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["launchctl", "list"], text=True, timeout=2,
        )
        if "com.loopmemory.openclaw" in (out or ""):
            for line in out.splitlines():
                if "com.loopmemory.openclaw" in line:
                    parts = line.split()
                    if parts and parts[0].lstrip("-").isdigit():
                        return True
    except Exception:
        pass
    return False


def run_doctor(_args) -> int:
    """`loop-memory doctor` — deep store-health check.

    Audit 2026-10-12.  Delegates to :mod:`loop_memory.doctor` which runs
    a small library of health checks against the live
    `MemoryStore` and renders a human-readable report.  Exits
    non-zero if any check is in 'error' state.  Source:
    cortexkit/magic-context doctor (MIT, 2026-10-12) +
    PerryLink/dsh-memento dsh-plugin-doctor (Apache-2.0,
    2026-10-12) — pattern only.
    """
    from . import doctor as _doctor_cli
    return _doctor_cli.run_doctor(list(_args))


def run_status(_args) -> int:
    """``loop-memory status`` — concise one-screen summary."""
    db_path = Path(DEFAULT_DB)
    print("loop-memory  v0.3.0")
    print(f"  db: {db_path}" + (f"  ({db_path.stat().st_size//1024} KB)" if db_path.exists() else ""))
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:7767/api/stats", timeout=1) as r:
            stats = json.loads(r.read().decode())
            print(f"  server: http://127.0.0.1:7767  ({stats['memories']} memories, "
                  f"{stats['sessions']} sessions, {stats['wiki_pages']} wiki)")
    except Exception:
        print("  server: not running  (start with `loop-memory serve`)")

    watcher = _detect_watcher()
    print(f"  watcher: {'running' if watcher else 'idle'}")
    clients = _detect_clients()
    installed = sum(1 for k in ("codex", "claude", "hermes", "openclaw") if clients[k]["installed"])
    wired = sum(1 for k in ("codex", "claude", "hermes") if clients[k]["installed"] and clients[k]["mcp_configured"])
    print(f"  clients: {installed} installed, {wired} MCP-wired")
    return 0


def run_openclaw_setup(_args) -> int:
    """``loop-memory openclaw-setup`` — install a launchd watcher for openclaw.

    Writes ``~/Library/LaunchAgents/com.loopmemory.openclaw.plist`` so
    that openclaw sessions + workspace/memory/*.md daily logs are
    ingested automatically after every conversation. Idempotent.
    """
    home = Path.home()
    openclaw_dir = home / ".openclaw"
    if not openclaw_dir.exists():
        print(_RED + "✘ ~/.openclaw not found — openclaw not installed." + _RESET)
        print("  Install clawx first, then re-run this command.")
        return 1

    candidates = [
        openclaw_dir / "agents" / "main" / "sessions",
        openclaw_dir / "sessions",
        openclaw_dir / "workspace" / "memory",
    ]
    existing = [str(c) for c in candidates if c.exists()]
    if not existing:
        print(_YELLOW + "⚠ ~/.openclaw exists but no transcript paths were found." + _RESET)
        print("  Expected at least one of:")
        for c in candidates:
            print(f"    {c}")
        return 1

    cli_path = shutil.which("loop-memory")
    label = "com.loopmemory.openclaw"
    plist_path = home / "Library" / "LaunchAgents" / f"{label}.plist"

    # Build ProgramArguments. If the CLI is on PATH we use it directly,
    # otherwise fall back to invoking the module via the current python
    # (user pip installs put loop-memory in ~/Library/Python/<v>/bin
    # which isn't on launchd's PATH).
    # Build a --watch <path> pair for every existing directory so the
    # watcher covers clawx sessions AND workspace/memory daily logs.
    watch_pairs = ""
    for p in existing:
        watch_pairs += f"    <string>--watch</string><string>{p}</string>\n"
    if cli_path:
        prog_args = (
            f"    <string>{cli_path}</string>\n"
            "    <string>hook</string>\n"
            "    <string>--source</string>\n"
            "    <string>openclaw</string>\n"
            + watch_pairs
        )
    else:
        py = sys.executable or "/usr/bin/env python3"
        prog_args = (
            f"    <string>{py}</string>\n"
            "    <string>-m</string>\n"
            "    <string>loop_memory.cli.main</string>\n"
            "    <string>hook</string>\n"
            "    <string>--source</string>\n"
            "    <string>openclaw</string>\n"
            + watch_pairs
        )

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'  <key>Label</key><string>{label}</string>\n'
        '  <key>ProgramArguments</key>\n'
        '  <array>\n'
        + prog_args +
        '  </array>\n'
        f'  <key>WorkingDirectory</key><string>{home}</string>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        '  <key>StandardOutPath</key><string>/tmp/loop_openclaw.log</string>\n'
        '  <key>StandardErrorPath</key><string>/tmp/loop_openclaw.log</string>\n'
        '</dict>\n'
        '</plist>\n'
    )
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(body, encoding="utf-8")
    print(_GREEN + "✓ wrote" + _RESET + f" {plist_path}")
    print(f"  watch paths: {', '.join(existing)}")

    # Reload via launchctl
    try:
        subprocess.run(["launchctl", "unload", str(plist_path)],
                       check=False, capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(plist_path)],
                       check=True, capture_output=True)
        print(_GREEN + "✓ loaded" + _RESET + " into launchd")
        print()
        print("Done. New openclaw sessions + workspace/memory/*.md daily logs will")
        print("be ingested automatically. Use `loop-memory doctor` to verify.")
    except subprocess.CalledProcessError as e:
        print(_YELLOW + "⚠ could not load via launchctl:" + _RESET, e)
        print(f"  Load it manually:  launchctl load -w {plist_path}")
    return 0
