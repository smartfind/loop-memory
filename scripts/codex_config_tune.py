"""Apply safe context-limits to ~/.codex/config.toml.

Without these, long Codex sessions grow unbounded and the app process
eats gigabytes of RAM because every tool output is replayed into the
model context on every turn. The defaults below keep a healthy headroom
for the current 258k context window; raise them only if your model
supports a larger window.
"""
from __future__ import annotations
import sys
import shutil
import datetime as dt
from pathlib import Path

CFG = Path.home() / ".codex" / "config.toml"
BAK = CFG.with_suffix(f".toml.bak.pre-tune-{int(dt.datetime.now().timestamp())}")

# Tunables. Conservative so they don't surprise users on first apply;
# any one of these alone would have prevented the 48 GB session.
PATCH = {
    "model_auto_compact_token_limit": 80000,
    "model_auto_compact_token_limit_scope": "conversation",
    "tool_output_token_limit": 4000,
    "project_doc_max_bytes": 50000,
}

def main(apply: bool = False) -> int:
    if not CFG.exists():
        print(f"❌ {CFG} not found", file=sys.stderr)
        return 1
    original = CFG.read_text()
    # If a value already exists, do not touch it. This script is idempotent.
    new_lines = []
    existing_keys = {k for k in PATCH if f"{k} =" in original}
    for k, v in PATCH.items():
        if k in existing_keys:
            print(f"  · {k} already set — leaving alone")
            continue
        new_lines.append(f"{k} = {_toml(v)}")
    if not new_lines:
        print("✓ All tunables already present, no changes needed")
        return 0
    if not apply:
        print("Dry-run. Re-run with --apply to write:")
        for ln in new_lines:
            print(f"  + {ln}")
        return 0
    shutil.copy2(CFG, BAK)
    CFG.write_text(original.rstrip() + "\n\n# Added by loop_memory codex-config-tune on "
                   + dt.date.today().isoformat() + "\n" + "\n".join(new_lines) + "\n")
    print(f"✓ Patched {CFG}")
    print(f"  Backup: {BAK}")
    return 0

def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)

if __name__ == "__main__":
    apply = "--apply" in sys.argv
    sys.exit(main(apply=apply))
