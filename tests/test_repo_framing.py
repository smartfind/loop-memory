"""Pin the framing of the project so it does not accidentally drift back
to a closed-list marketing pitch.

The first impression of Loop Memory lives in three places:

1. The GitHub repository ``About`` description (sidebar on the repo
   page + meta tag for scrapers / PyPI listings).
2. The headline block in ``README.md`` above the badges.
3. The first body paragraph under ``## What it does``.

All three must frame loop-memory as a **general-purpose, agent-agnostic**
product — never as a closed list of 4 named agents. Update both the
strings in the repo and the assertions here together when broadening
(or narrowing) the supported agent roster.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
README = (REPO / "README.md").read_text()


GITHUB_DESCRIPTION = (
    "Local-first, agent-agnostic memory system that closes the agent loop "
    "on any AI agent. Watches any agent's transcript directory on disk "
    "and distils long sessions into a curated wiki that re-injects into "
    "the next turn. Out of the box: hooks for Codex / Claude / Hermes / "
    "OpenClaw; SDK + generic watcher CLI for the rest."
)
def _gh_api_description() -> str:
    """Fetch the live GitHub About description; only runs when the
    test environment has a working ``gh`` CLI with repo read access.

    Skips (rather than fails) when we can't reach the API so the
    regression suite still passes for forks and offline contributors.
    """
    import shutil
    import subprocess
    if shutil.which("gh") is None:
        pytest.skip("gh CLI not available")
    res = subprocess.run(
        ["gh", "api", "repos/smartfind/loop-memory", "--jq", ".description"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        pytest.skip(f"gh api failed: {res.stderr.strip()}")
    return res.stdout.strip()


def test_github_repo_description_is_agent_agnostic() -> None:
    desc = _gh_api_description()
    assert desc == GITHUB_DESCRIPTION, (
        f"GitHub About description drifted from the canonical text.\n"
        f"  live:    {desc!r}\n"
        f"  pinned:  {GITHUB_DESCRIPTION!r}\n"
        f"Update via: gh api -X PATCH repos/smartfind/loop-memory "
        f"-f description=... when broadening the framing."
    )


def test_github_repo_description_uses_any_language() -> None:
    desc = _gh_api_description()
    lowered = desc.lower()
    assert "any ai agent" in lowered or "any agent" in lowered, (
        "GitHub About must lead with 'any [AI] agent', not a closed list"
    )


def test_readme_lead_uses_any_agent_framing() -> None:
    """The README headline above the badges must not enumerate a closed
    agent list as if it were the complete set of supported clients.
    """
    # Extract the lead block (between the title and the badge row).
    lead_start = README.index("# Loop Memory")
    badge_start = README.index("[![CI]")
    lead = README[lead_start:badge_start]

    assert "any agent" in lead.lower() or "every ai agent" in lead.lower(), (
        "README lead must lead with 'any agent' / 'every AI agent'"
    )
    assert "SDK" in lead or "generic watcher" in lead, (
        "README lead must mention the SDK / generic watcher CLI so the "
        "naming of 4 agents reads as examples, not as a closed list"
    )


def test_readme_what_it_does_breaks_the_closed_list_impression() -> None:
    """The first body paragraph under ``## What it does`` must include
    either an explicit non-shipped agent (e.g. Aider / Cursor) or the
    phrase 'generic watcher CLI' so it does not look like Loop Memory
    supports only the 4 shipped hooks.
    """
    body_start = README.index("## What it does")
    next_section = README.index("## Install", body_start)
    body = README[body_start:next_section]

    assert "generic watcher cli" in body.lower(), (
        "Body under 'What it does' must mention the generic watcher CLI"
    )
    outsider_agents = ("Aider", "Cursor", "Copilot", "Cline", "Continue", "Goose")
    assert any(a in body for a in outsider_agents), (
        f"Body must name at least one agent we *don't* ship a hook for "
        f"({', '.join(outsider_agents)}) so the list reads as examples, "
        f"not the full set"
    )


def test_comparison_table_uses_generic_watcher() -> None:
    """The Multi-source capture row in the competitive table must use
    generic language, not a closed list.
    """
    table_row = next(
        (line for line in README.splitlines()
         if line.startswith("| Multi-source capture")),
        None,
    )
    assert table_row is not None, "competitive comparison row missing"
    lowered = table_row.lower()
    assert "generic watcher" in lowered or "any agent" in lowered, (
        f"Multi-source capture row must be generic; got: {table_row!r}"
    )


def test_readme_does_not_pitch_4_agents_as_complete_set() -> None:
    """Belt-and-braces: assert the *exact* old closed-list sentence is
    gone from the README. If someone copy-pastes the old marketing
    copy back in, this test fails.
    """
    forbidden = (
        "Each agent (Codex CLI, Claude Code, Hermes, OpenClaw / clawx, …) "
        "drops its transcripts onto disk"
    )
    assert forbidden not in README, (
        f"Found the old closed-list sentence. Re-add 'Any agent ... works' "
        f"framing. The old sentence forbids: {forbidden!r}"
    )


def test_github_topics_include_generic_terms() -> None:
    """The GitHub repo topic list must include broad 'memory / agent /
    local-first' tags so it surfaces in searches for adjacent agents
    (not just the 4 we ship hooks for).
    """
    import shutil
    import subprocess
    if shutil.which("gh") is None:
        pytest.skip("gh CLI not available")
    res = subprocess.run(
        ["gh", "api", "repos/smartfind/loop-memory/topics", "--jq", ".names | join(\",\")"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        pytest.skip(f"gh api failed: {res.stderr.strip()}")
    topics = {t.strip() for t in res.stdout.split(",") if t.strip()}
    needed = {"agent", "ai-agent", "memory", "long-term-memory", "local-first", "agent-loop", "agentic"}
    missing = needed - topics
    assert not missing, (
        f"GitHub topics missing general-purpose tags: {missing}. Add via: "
        f"gh api -X PUT repos/smartfind/loop-memory/topics "
        f"-f names[]=agent -f names[]=ai-agent -f names[]=memory ..."
    )

def test_github_repo_description_includes_agent_loop_keyword() -> None:
    """Pin the ``agent loop`` keyword in the GitHub ``About`` description.

    Users find this project by searching for "agent loop", "agent-loop",
    "agentic loop", "memory loop" — make sure the sidebar description
    surfaces for those queries. If someone rewrites the description
    without those keywords, this test fails.

    Accepts either the bare phrase ("agent loop") or the hyphenated
    topic-tag form ("agent-loop") since both are valid search tokens.
    """
    desc = _gh_api_description()
    lowered = desc.lower()
    has_phrase = ("agent loop" in lowered) or ("agent-loop" in lowered)
    assert has_phrase, (
        "GitHub About description must include 'agent loop' / 'agent-loop' "
        "so the project surfaces for searches like 'agent loop memory'."
        f"current: {desc!r}"
    )


def test_readme_lead_includes_agent_loop_keyword() -> None:
    """Pin the ``agent loop`` keyword in the README headline so the
    repository's first impression mirrors the GitHub ``About`` sidebar.
    """
    lead_start = README.index("# Loop Memory")
    badge_start = README.index("[![CI]")
    lead = README[lead_start:badge_start].lower()
    assert ("agent loop" in lead) or ("agent-loop" in lead), (
        "README lead must mention 'agent loop' / 'agent-loop' so it "
        "matches the GitHub About description keyword set."
    )

