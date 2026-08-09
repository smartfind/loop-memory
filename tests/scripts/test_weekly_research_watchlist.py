"""Regression test for the weekly-research project watchlist.

The agent that runs the weekly research cycle is told the list of
peer projects to monitor via a prompt embedded in
``scripts/weekly_research_update.sh``. This test pins that list so
future edits don't silently drop a project from the watchlist.

To update the watchlist, edit both the script prompt and this file.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "weekly_research_update.sh"
SOURCE = SCRIPT.read_text()


# Each entry pins one peer project the research cycle should keep
# monitoring. Format: (substring that MUST appear in the script prompt,
# optional note used by humans). Tests fail if any of these are missing.
WATCHLIST = [
    ("Mem0", "Mem0 self-hosted server / SDK"),
    ("Letta", "Letta cloud / open-source memory server"),
    ("Zep/Graphiti", "Zep + Graphiti temporal + knowledge graph memory"),
    ("LangMem", "LangMem (langchain-ai) procedural memory"),
    ("OpenMemory", "OpenMemory by CaviraOSS"),
    ("TencentDB Agent Memory", "TencentCloud/tencentdb-agent-memory (watch only)"),
    (
        "TencentCloud/tencentdb-agent-memory",
        "TencentDB Agent Memory canonical repo reference",
    ),
]


def test_weekly_research_watchlist_contains_all_peers() -> None:
    for needle, label in WATCHLIST:
        assert needle in SOURCE, (
            f"weekly_research_update.sh must mention {label!r} "
            f"(looking for substring {needle!r}) — update the prompt "
            f"when adjusting the watchlist, and update WATCHLIST here"
        )


def test_weekly_research_does_not_silently_adopt_tencentdb() -> None:
    """TencentDB Agent Memory is on the watchlist but explicitly
    *not* being adopted; the prompt must keep that distinction so the
    research agent doesn't propose a port.
    """
    assert "currently NOT being adopted" in SOURCE, (
        "TencentDB must remain flagged as watch-only in the prompt; "
        "remove this test when/if we decide to ship Skill / CodeGraph / "
        "Memory Proxy inspired by TencentDB."
    )


def test_weekly_research_prompt_remains_in_heredoc() -> None:
    """Sanity-check that the prompt block is still embedded as a heredoc
    in the shell script (the structure the launchd watcher relies on).
    """
    assert "PROMPT=$(cat <<'EOF'" in SOURCE, (
        "weekly_research_update.sh must keep its PROMPT in a heredoc so "
        "the agent receives the full multi-line instructions"
    )
