# Loop Memory

[![CI](https://img.shields.io/github/actions/workflow/status/smartfind/loop-memory/tests.yml?branch=main&style=flat-square)](https://github.com/smartfind/loop-memory/actions)
[![PyPI](https://img.shields.io/pypi/v/loop-memory.svg?style=flat-square)](https://pypi.org/project/loop-memory/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/loop-memory?style=flat-square)](https://pypi.org/project/loop-memory/)
[![Zero deps](https://img.shields.io/badge/dependencies-0-success.svg?style=flat-square)](pyproject.toml)

> A local **Loop Engineering** memory system for any LLM. Auto-captures
> conversations from Codex / Claude / Hermes, scores them by time, lets
> you browse and consolidate everything from a single web page.

---

## What it does

Loop Memory gives you **a persistent memory loop for every local LLM
tool you use**, not just one product. Conversations end, transcripts
land on disk, Loop Memory pulls them into a SQLite-backed store, scores
each memory by *importance × recency*, lets a background job dedupe +
GC + rescore, and shows the lot in a small local web UI.

```
   Claude Code ─┐
   Codex CLI   ─┼──►  loop-memory ingest / watcher  ──►  SQLite store
   Hermes      ─┘                                       (sessions + memories)
                                                         │
                                                         ▼
                          search, time filter, score filter, ranking
                                                         │
                         ┌───────────────────────────────┴────────────────┐
                         ▼                                                ▼
                  loop-memory serve                          loop-memory consolidate
                  (web UI on :7767)                          (rescore + GC + dedupe)
```

---

## Install

```bash
pip install loop-memory                          # core: zero deps
pip install 'loop-memory[serve]'                  # + FastAPI web UI
pip install 'loop-memory[openai]'                 # + OpenAI client
pip install 'loop-memory[all]'                    # everything
```

---

## Quickstart

```bash
# 1. Import everything that already lives on your disk
loop-memory ingest codex          # ~/.codex/sessions/*.json
loop-memory ingest claude         # ~/.claude/**/*.jsonl
loop-memory ingest hermes         # ~/.hermes/**/*.jsonl

# 2. Look at it
loop-memory serve --port 7767     # open http://127.0.0.1:7767

# 3. Make it run on a timer
#    (see docs/auto-capture.md for launchd / systemd / cron snippets)
loop-memory consolidate          # rescore + GC + dedupe
```

---

## Architecture

See [docs/architecture.md](docs/architecture.md) for a layered view of the
subpackages, the 5-stage evolution pipeline, the request lifecycle, and how
secrets and settings are separated between the SQLite store and a local
permission-restricted secrets file.

---


## After install: 30-second setup

```bash
# Show me what's installed, what's wired, what's broken.
loop-memory doctor

# Auto-configure MCP + SessionStart hooks for every detected CLI
# (Codex CLI, Claude Code, Hermes).
loop-memory install-hooks

# Install the openclaw/clawx auto-ingest watcher (launchd on macOS).
loop-memory openclaw-setup

# Run it on a schedule — web UI → ⚙ Model → set "every day 03:00".
loop-memory serve --port 7767   # → http://127.0.0.1:7767
```

The web UI also has a **🔍 Run doctor** panel under the kebab menu
(⌘D) that shows the same green/red diagnostic screen inline.

---

## Auto-capture (after every conversation)

A new conversation ends → its transcript file lands in a watched
directory → the watcher ingests it → it shows up in the UI. Three
flavors:

| Tool                       | Watch                                           |
| -------------------------- | ----------------------------------------------- |
| Codex CLI                  | `loop-memory hook --source codex  --watch ~/.codex/sessions`   |
| Claude Code                | `loop-memory hook --source claude --watch ~/.claude`           |
| Hermes                     | `loop-memory hook --source hermes --watch ~/.hermes`           |
| OpenClaw (clawx)           | `loop-memory hook --source openclaw --watch ~/.openclaw/agents/main/sessions` — also ingests `workspace/memory/*.md` daily logs |

Three of these in a `tmux` session, or persisted via launchd, keeps
your memory store fresh without any clicks. Run `loop-memory
consolidate` on an hourly cron to keep the scoring healthy.

See [docs/auto-capture.md](docs/auto-capture.md) for ready-to-paste
launchd + systemd + cron snippets.
---

## Dashboard + Evolution consolidator (看板 + 进化式蒸馏)

The Dashboard tab gives you a live, at-a-glance view of the memory
pipeline and lets you steer it.

- **4 KPI cards** — raw memory count, distilled wiki count, average
  score, total recall events (real-time, auto-refresh every 8s).
- **5-stage data-flow animation** — score → cluster → distill → wiki
  → memo. Click any node to drill into the items that flowed through
  it last run. The wave path on top pulses to suggest motion; nodes
  pulse on hover.
- **Drill-down panel** — every item has 👍 / 👎 buttons that feed the
  evolution loop. Negative feedback lowers the memory's importance;
  positive bumps it. Both update the "most recalled memories" list.
- **Evolution run button** — invokes the 5-stage Evolution
  Consolidator with whatever provider is currently configured.

### Evolution Consolidator (replaces the old single-pass one)

A hierarchical, signal-aware distillation pipeline designed to keep
your knowledge base tight and increasingly aligned with your real
preferences over time.

| Stage | What it does |
| ----- | ------------ |
| 1. Signal-Aware Scoring    | Blends `importance × recency` with `recall_count` (+0..0.10) and `negative` feedback (-0..0.15), so items the user actually uses float to the top. |
| 2. Semantic Batching       | Greedy cosine clustering using a hashed embedding; clusters ≤15 items each, threshold 0.35. |
| 3. Per-Cluster Distillation | LLM returns per-row `keep / importance / distill / tags` actions. Row-level rewrites only when the LLM is confident. |
| 4. Hierarchical Wiki       | Cluster summaries + existing wiki + the **evolution memo** feed the LLM, which produces / updates pages bucketed into `preferences / decisions / projects / domain / feedback`. Slugs are stable, so re-running merges. |
| 5. Evolution Memo          | Persists `{rescored, dropped, wiki_created, wiki_updated, notes}` for the last run; next run's Stage-4 prompt includes it so the LLM keeps learning the user's preferences across runs. |

Run it manually:

```bash
loop-memory consolidate           # legacy single-pass
curl -X POST http://127.0.0.1:7767/api/admin/evolution/run   # 5-stage
```



### Scoring v2: time × usage × feedback

The score of every memory is a weighted blend of **four** components,
not just importance × recency:

| Component | Weight | What it measures |
| --------- | ------ | ---------------- |
| `importance` | 0.40 | Original LLM/original importance in [0, 1] |
| `recency`    | 0.25 | Time decay: `½^(age / half_life)`, default half_life 30 days |
| `usage`      | 0.25 | `log1p(recall_count)/log1p(100) × recency_of_last_recall` |
| `feedback`   | 0.10 | `tanh((positive - negative) / 3)` — sticky (no time decay) |

The blend is normalised to [0, 1]. **What this means in practice**:
recent + useful memories float up; old + unused memories sink;
memories the user explicitly 👍 stay high; 👎 ones stay low even
if they were popular once.

API endpoints to inspect the breakdown:

```bash
curl localhost:7767/api/memories/<id>/score              # 4 components
curl localhost:7767/api/pipeline/score-distribution     # 10-bin histogram
curl localhost:7767/api/pipeline/decay-stats            # age buckets × avg score
curl -XPOST 'localhost:7767/api/admin/bump-recall?ids=<id>'  # simulate LLM recall
```

### Dashboard v2: real animation, real charts

The Dashboard tab has been rebuilt end-to-end:

- **5 KPI cards** with live sparklines (60-sample rolling history).
- **Active-stage card** — shows the pipeline stage currently running,
  switching automatically as `pipeline_runs` update.
- **Animated data flow** — particle dots travel left-to-right along
  the SVG path whenever a stage is running; nodes pulse with the
  active stage highlighted. Click any node to drill down.
- **Score distribution histogram** — 10 bins of v2 score, hover for
  exact counts.
- **Time-decay chart** — bars are count per age bucket, the line on
  top plots average score so you can *see* the decay curve.
- **Per-memory score breakdown** — every drill-down item has a
  "why?" button that expands a 4-bar breakdown (importance /
  recency / usage / feedback).
- **↻ bump button** on each item — lets you mark a memory as
  "just consulted by the LLM" so its usage component goes up and
  it ranks higher next time.


### Feedback loop

User signals close the loop:

- 👍 on a drill-down item → `positive++`, `importance += 0.05`
- 👎 → `negative++`, `importance -= 0.05`
- Every `recall()` / search bumps `recall_count` on the returned
  rows so the next Stage-1 ranks them higher.

## Auto-feedback into every LLM client (反哺)

Distilled knowledge is only useful if your LLM tools can actually
read it. Loop Memory ships with three zero-dep commands that wire
the memory store into Codex CLI, Claude Code and Hermes
automatically:

| Command                          | What it does                                                                 |
| -------------------------------- | ---------------------------------------------------------------------------- |
| `loop-memory install-hooks`      | Auto-detect `~/.codex`, `~/.claude`, `~/.hermes` and write MCP + SessionStart hook configs in place. Idempotent — re-run any time. |
| `loop-memory inject [query]`     | Print a `# Long-term memory context` markdown block (distilled wiki + recent relevant memories) for a SessionStart hook. |
| `loop-memory mcp`                | Run the **stdio MCP server** that exposes `recall` / `list_wiki` / `get_wiki` / `recent_memories` / `wiki_summary` to any MCP-aware client. |

Quick setup on a fresh machine:

```bash
pip install loop-memory
loop-memory install-hooks       # writes ~/.codex/config.toml + ~/.claude/{mcp.json,settings.json} + ~/.hermes/mcp.json
# restart Codex / Claude Code / Hermes and the next session will:
#   1) auto-inject the distilled wiki as the first user message (SessionStart hook)
#   2) expose `recall` / `list_wiki` / `get_wiki` MCP tools so the model can pull more on demand
```

Manual smoke-test without restarting the client:

```bash
loop-memory inject                       # dumps the warm-start block to stdout
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"wiki_summary"}}\n' \
  | loop-memory mcp                      # round-trips JSON-RPC over stdio
```

The MCP server speaks JSON-RPC 2.0 over newline-delimited stdin/stdout,
uses no third-party deps, and is safe to launch per-client (Claude Code,
Codex CLI, Hermes each spawn their own process). OpenClaw is detected but
currently needs `loop-memory hook --source openclaw --watch
~/.openclaw/sessions &` to start its watcher.


---

## Web UI

`loop-memory serve` opens a small local page at
`http://127.0.0.1:7767` with four primary views:

- **Timeline**: searchable session history and scored memories from every client.
- **Dashboard**: lifecycle, source health, distillation progress, weekly report,
  contradictions, audit data, and the end-to-end memory architecture.
- **Wiki**: distilled, editable knowledge pages with export and Ask workflows.
- **Knowledge graph**: an interactive globe built from distilled Wiki knowledge.

Top-right actions provide one-click import, re-scoring, AI consolidation, model
configuration, scheduling, language switching, and light/dark themes.

---

## Time-weighted scoring

Every memory carries a `score ∈ [0, 1]` recomputed from:

```
score = 0.35 · importance + 0.65 · recency
recency = ½ ^ (age / half_life)
```

`half_life` defaults to 30 days, configurable via
`consolidate(half_life_days=...)`. The UI shows the score as a
percentage; use `?min_score=0.85` to see only high-relevance memories.

---

## Programmatic use

```python
from loop_memory import MemoryStore
from loop_memory.ingest.loader import get_loader
from loop_memory.ingest.pipeline import MemoryPipeline
from loop_memory.backends.embedding import HashingEmbedder
from loop_memory.jobs.consolidate import Consolidator

store = MemoryStore("~/.loop_memory/loop_memory.db")
pipeline = MemoryPipeline(store, embedder=HashingEmbedder(dim=128))

loader = get_loader("claude")
for path in loader.discover():
    session = loader.load_one(path)
    if session:
        pipeline.run(session)

# background-style consolidation
report = Consolidator(store, embedder=HashingEmbedder(dim=128)).run()
print(report)
# ConsolidateReport(rescored=15, gc_removed=0, merged=0, elapsed_ms=2.88)
```

Or just keep using the engine inside a Python process:

```python
from loop_memory import LoopEngine, EchoLLM, HashingEmbedder
engine = LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=128))
print(engine.turn("Hi! I'm Mia and I love matcha.").reply)
```

---

## The four-stage loop

Even though v0.2 is built around local storage, the original
`Retrieve → Generate → Reflect → Store` loop engine is still here:

| Stage      | Default impl                   | Replace with                       |
| ---------- | ------------------------------ | ---------------------------------- |
| RETRIEVE   | cosine + importance × recency  | any `VectorStore` (Chroma, FAISS…) |
| GENERATE   | any `LLMClient`                | OpenAI, Anthropic, local, …        |
| REFLECT    | regex fact extractor          | an LLM-based reflector             |
| STORE      | short-term + episodic + LTM    | persistent store via extras        |

---

## Project layout

```
loop_memory/
  loop_memory/
    memory/types.py            # MemoryItem + 4 tiers
    backends/embedding.py      # BaseEmbedder, HashingEmbedder, IdentityEmbedder
    backends/vector_store.py   # VectorStore protocol + InMemory / Chroma
    backends/sentence_embedder.py  # optional sentence-transformers
    llm/base.py                # LLMClient protocol + EchoLLM + helpers
    llm/openai_adapter.py      # optional OpenAI client
    engine/loop.py             # the Retrieve → Generate → Reflect → Store loop
    engine/reflect.py          # reflection & summarization passes
    storage/sqlite_store.py    # persistent SQLite-backed MemoryStore
    ingest/loader.py           # CodexLoader, ClaudeLoader, HermesLoader
    ingest/pipeline.py         # session → MemoryStore
    jobs/consolidate.py        # background rescore + GC + dedupe
    serve/app.py               # FastAPI app for the local web UI
    serve/static/index.html    # the page
    serve/watcher.py           # filesystem watcher for auto-capture
    cli/main.py                # CLI entrypoint (chat / stats / ingest / consolidate / serve / hook)
    examples/demo.py           # runnable, zero-API-key demo
    py.typed
  tests/                       # 92 unit tests, zero deps
  docs/auto-capture.md         # launchd / systemd / cron recipes
```

---

## Run the tests

```bash
python -m unittest discover tests -v
```



## Using distilled knowledge in your clients

After running `loop-memory consolidate` (or letting the scheduler do it), your
memories get distilled into durable **wiki pages**. Three ways to use them in
Claude / Codex / Hermes / OpenClaw:

### 1. Quick paste — `loop-memory ask`

Works from any terminal, **no server required**:

```bash
loop-memory ask "what does the user prefer for X?"
```

Prints a paste-ready context block to stdout. Put it as the first message of a
new session in any LLM client.

### 2. Whole wiki export

```bash
loop-memory export            # writes ~/loop-memory-export-<date>.md
loop-memory export --out ~/Notes/user.md --q "preferences"
```

Or in the UI: open the **Wiki** tab → click **⇩ Export**. A markdown file
downloads; paste it into your daily journal or as a system prompt.

### 3. Per-page "Copy as context" in the UI

Each wiki card has a `⎘` button that copies a single distilled page formatted
as background context — ready to paste as the system prompt of a fresh
Codex / Claude / Hermes session.

### 4. Auto-context (MCP-aware clients)

If you ran `loop-memory install-hooks`, Codex / Claude Code / Hermes will
automatically pull relevant memories via the MCP server. OpenClaw does not
support MCP — use `loop-memory ask` instead.

### Manual trigger

Click the **⚡ Run now** button (top-right) or run:

```bash
loop-memory consolidate-now    # ask the running server to start a pass right now
```

This uses your configured model, batch size, and provider — same as the
scheduled runs.

## License

[MIT](LICENSE)
