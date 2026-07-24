# Codex App — Context-Bloat Survival Guide

Codex sessions are append-only JSONL logs at `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<id>.jsonl`. Every turn, tool call, and tool output is replayed into the model context on the next turn. After a few hundred turns, the file balloons past a gigabyte and the desktop app process pins tens of GB of RAM — eventually freezing the host.

Loop Memory cannot shrink Codex's in-process state directly. What it can do is:

1. **Patch your `~/.codex/config.toml` so Codex auto-compacts.** This is the single biggest win and ships with the project.
2. **Generate a tight 6 KB distilled digest** that the assistant can preload instead of replaying history.
3. **Ship a session auditor** so you can see which sessions are at risk *before* they crash the host.

## Quick fix — apply in 30 seconds

```bash
python3 -m loop_memory.scripts.codex_config_tune --apply
```

This adds four lines to `~/.codex/config.toml` (with a timestamped backup):

```toml
model_auto_compact_token_limit = 80000
model_auto_compact_token_limit_scope = "conversation"
tool_output_token_limit = 4000
project_doc_max_bytes = 50000
```

- `model_auto_compact_token_limit` — Codex runs `/compact` automatically whenever the conversation token count crosses this threshold. Pick a value comfortably below your model's context window (the default 80k works for the 258k MiniMax-M3 window; raise it for larger models).
- `tool_output_token_limit` — hard cap on the size of any single tool output replayed into context. The single biggest source of bloat in practice; 4k tokens is enough for almost any tool call.
- `project_doc_max_bytes` — cap on the project doc Codex reads at session start (50 kB).

The script is **idempotent**: re-running it leaves existing values untouched.

## Audit your existing sessions

```bash
python3 -m loop_memory.scripts.codex_session_audit
```

Sample output:

```
Audited 24 sessions under /Users/smartcodex/sessions  (context window = 258,400 tokens)

STATUS            PEAK_IN   SIZE_MB   LINES  TOOL_OUT  PATH
-----------------------------------------------------------
critical    1,179,648,193     280.2  37,708     9,260  /…/rollout-2026-07-10T22-53-…jsonl
critical      193,317,936     180.0   6,573     1,516  /…/rollout-2026-06-26T21-12-…jsonl
…
```

Sessions in `critical` or `high` status have replayed far more tokens than the model can hold in one context — these are the candidates for `/compact`, `/clear`, or `codex archive <id>`.

## Preload a distilled digest

After the auto-compact is in place, you can also reduce how much *new* context the model needs by pointing it at a small Loop Memory digest:

```bash
python3 -m loop_memory.cli.main digest --out ~/.loop_memory/AGENTS.md --max-chars 8000
```

Then add a `loop-memory-context` skill to your Codex skills directory (see `skills/loop-memory-context/SKILL.md`). At session start the assistant will read the digest once and keep its facts in scope for the rest of the session — replacing the need to replay history at all.

## When `/compact` is not enough

`/compact` summarises the conversation into a single rolling summary. If the session still grows after `/compact`, the conversation has fundamentally too much state for the model to track. At that point:

- **Open a new session** and load the digest instead.
- **`codex archive <session-id>`** to retire the bloated session from the active picker.
- **Reduce reasoning effort** in `~/.codex/config.toml`: `model_reasoning_effort = "medium"` cuts reasoning trace size in half.
- **Disable unused plugins/skills**: each loaded skill adds its instructions to every turn's context.

## How Loop Memory helps long-term

The project watches your Codex sessions and ingests them into a long-term store (SQLite at `~/.loop_memory/loop_memory.db`). The consolidator periodically distills those raw memories into a few wiki pages — small, dense, contradiction-checked. The digest is built from those wiki pages.

So even after the active Codex session is closed and archived, the **distilled knowledge** is preserved in Loop Memory and ready to be loaded into your next session in 6 KB instead of 280 MB.

## Reference

- `loop_memory/scripts/codex_config_tune.py` — applies the four tunables.
- `loop_memory/scripts/codex_session_audit.py` — audits existing sessions.
- `loop_memory/cli/commands/read.py :: run_digest` — emits the compact digest.
- `loop_memory/skills/loop-memory-context/SKILL.md` — Codex skill that loads the digest.
