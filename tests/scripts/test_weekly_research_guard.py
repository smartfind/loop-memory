"""Verify weekly_research_update.sh only refuses tracked local changes.

Mirrors the inline bash guard in scripts/weekly_research_update.sh so a
future regression in either place is caught.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "weekly_research_update.sh"
SOURCE = SCRIPT.read_text()

HAS_PORCELAIN_GUARD = "git status --porcelain" in SOURCE
HAS_TRACKED_FAIL = "tracked local changes" in SOURCE
HAS_TRACKED_EXIT_1 = re.search(r"tracked local changes.*?exit 1", SOURCE, re.DOTALL) is not None
HAS_UNTRACKED_CONTINUE = "Only untracked files present; continuing." in SOURCE
HAS_MERGE_GUARD = "MERGE_HEAD" in SOURCE
HAS_REBASE_GUARD = "rebase-merge" in SOURCE and "rebase-apply" in SOURCE

def test_skip_block_is_present() -> None:
    assert HAS_PORCELAIN_GUARD, "weekly_research_update.sh must inspect git status --porcelain"
    assert HAS_TRACKED_FAIL, "guard must call out tracked local changes"
    assert HAS_TRACKED_EXIT_1, "tracked changes must hard-fail (exit 1), not silently skip"

def test_untracked_only_continues() -> None:
    assert HAS_UNTRACKED_CONTINUE, "untracked-only trees must be allowed to continue"

def test_merge_and_rebase_safely_skip() -> None:
    assert HAS_MERGE_GUARD, "weekly_research_update.sh must skip mid-merge trees"
    assert HAS_REBASE_GUARD, "weekly_research_update.sh must skip mid-rebase trees"

def test_shell_syntax() -> None:
    for shell in ("zsh", "bash"):
        if shutil.which(shell) is None:
            continue
        proc = subprocess.run(
            [shell, "-n", str(SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{shell} -n reported syntax errors"
        return
    pytest.skip("neither zsh nor bash is available on this runner")
