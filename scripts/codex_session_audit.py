"""Audit Codex session files and warn before they bloat the host.

Each Codex session is a JSONL append-only log under
``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<...>.jsonl``. Long
sessions grow into the hundreds of MB because every tool output is
replayed into the model context on every turn. This script reads the
``token_count`` events Codex already emits and prints a per-session
report so the user can decide which ones to ``/compact`` or archive.

Usage::

    python3 -m loop_memory.scripts.codex_session_audit           # all sessions
    python3 -m loop_memory.scripts.codex_session_audit --top 10  # top 10 by peak tokens
    python3 -m loop_memory.scripts.codex_session_audit --json    # machine-readable
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

DEFAULT_ROOT = Path.home() / ".codex" / "sessions"
CTX_WINDOW_DEFAULT = 258_400  # tokens, matches the current MiniMax-M3 window

def iter_sessions(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("rollout-*.jsonl")):
        yield path

def audit_file(path: Path) -> dict:
    """Read a session JSONL once and pull token_count peaks + size.

    We avoid loading the full file into memory — the line iterator
    only carries the line we just parsed.
    """
    info = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "lines": 0,
        "peak_input_tokens": 0,
        "peak_total_tokens": 0,
        "final_input_tokens": 0,
        "first_ts": None,
        "last_ts": None,
        "tool_output_count": 0,
        "tool_output_bytes": 0,
    }
    peak_inp = 0
    peak_tot = 0
    final_inp = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            info["lines"] += 1
            try:
                o = json.loads(line)
            except Exception:
                continue
            ts = o.get("timestamp")
            if ts:
                if not info["first_ts"]:
                    info["first_ts"] = ts
                info["last_ts"] = ts
            p = o.get("payload", {})
            if o.get("type") == "response_item" and p.get("type") == "function_call_output":
                info["tool_output_count"] += 1
                # Approximate: line bytes minus JSON envelope
                info["tool_output_bytes"] += len(line)
            if p.get("type") == "token_count":
                tu = p.get("info", {}).get("total_token_usage", {})
                inp = int(tu.get("input_tokens", 0) or 0)
                tot = int(tu.get("total_tokens", 0) or 0)
                if inp > peak_inp:
                    peak_inp = inp
                if tot > peak_tot:
                    peak_tot = tot
                final_inp = inp
    info["peak_input_tokens"] = peak_inp
    info["peak_total_tokens"] = peak_tot
    info["final_input_tokens"] = final_inp
    info["size_mb"] = round(info["size_bytes"] / (1024 * 1024), 2)
    return info

def severity(info: dict, ctx: int) -> str:
    peak = info.get("peak_input_tokens", 0) or 0
    if peak > ctx * 5:
        return "critical"   # 5x overflow → process memory stress
    if peak > ctx * 2:
        return "high"
    if peak > ctx:
        return "warning"
    return "ok"

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="codex sessions root")
    ap.add_argument("--top", type=int, default=20, help="show top N sessions by peak tokens")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--ctx-window", type=int, default=CTX_WINDOW_DEFAULT,
                    help="model context window in tokens (default: 258400)")
    args = ap.parse_args(argv)

    rows = [audit_file(p) for p in iter_sessions(args.root)]
    if not rows:
        print(f"No sessions found under {args.root}")
        return 0
    rows.sort(key=lambda r: r.get("peak_input_tokens", 0), reverse=True)

    if args.json:
        json.dump(rows[: args.top], sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    print(f"Audited {len(rows)} sessions under {args.root}  (context window = {args.ctx_window:,} tokens)\n")
    header = f"{'STATUS':<10} {'PEAK_IN':>14} {'SIZE_MB':>9} {'LINES':>7} {'TOOL_OUT':>9}  PATH"
    print(header)
    print("-" * len(header))
    for r in rows[: args.top]:
        sev = severity(r, args.ctx_window)
        print(f"{sev:<10} {r['peak_input_tokens']:>14,} "
              f"{r['size_mb']:>9.1f} {r['lines']:>7,} "
              f"{r['tool_output_count']:>9,}  {r['path']}")
    crit = [r for r in rows if severity(r, args.ctx_window) in ("critical", "high")]
    if crit:
        print("\n⚠️  Critical / high sessions should be handled immediately:")
        print("   • Run `/compact` in the Codex UI for each affected session, or")
        print("   • Run `codex archive <session-id>` to retire a session, or")
        print("   • Open a fresh session and inject the loop_memory digest instead:")
        print("     `python3 -m loop_memory.cli.main memory-digest --out ~/.codex/AGENTS.md`")
        print("   • Set `model_auto_compact_token_limit` in ~/.codex/config.toml")
        print("     (use `python3 -m loop_memory.scripts.codex_config_tune --apply`)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
