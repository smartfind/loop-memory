# Architecture

A short map of the codebase so new contributors (and future you) can find their
way around without grepping 8,000 lines of Python.

## Layered view

```mermaid
flowchart TD
    CLI[cli/] --> Engine[engine/]
    CLI --> Storage[storage/]
    CLI --> Ingest[ingest/]
    CLI --> Serve[serve/]
    Engine --> LLM[llm/]
    Engine --> Memory[memory/]
    Engine --> Storage
    Serve --> Engine
    Serve --> Storage
    Serve --> Jobs[jobs/]
    Serve --> Graph[graph/]
    Jobs --> LLM
    Jobs --> Storage
    Jobs --> Embed[backends/]
    Graph --> Storage
    Ingest --> Storage
    Ingest --> Embed
    Security[security/] --> Storage
    MCP[mcp/] --> Storage
    MCP --> Engine
    SDK[sdk.py + sdk_extensions.py] --> Storage
    Export[export/] --> Storage
    Jobs --> Graph
```

## Subpackage responsibilities

- **`engine/`** — the core loop. `LoopEngine` drives a turn (read short-term,
  decide what to remember, write to long-term). Pure logic; talks to LLM and
  embedder only.
- **`memory/`** — dataclasses for `MemoryItem`, `WikiPage`, `Entity`, etc. The
  vocabulary of the system.
- **`storage/`** — `MemoryStore`: SQLite-backed persistence. Single source of
  truth for sessions, memories, entities, relations, wiki pages, settings,
  pipeline runs, signals.
- **`backends/`** — pluggable embedders (hashing, sentence-transformers) and
  vector stores (in-memory, Chroma).
- **`llm/`** — `LLMClient` protocol + providers (OpenAI-compatible, Anthropic,
  Ollama, rule-based fallback) and a validated `default_config()` shape.
- **`ingest/`** — convert external transcript formats (Codex, Claude, Hermes,
  generic JSONL) into the common `IngestedSession` and run them through the
  `MemoryPipeline`.
- **`jobs/`** — background work. `Consolidator` (rescore + gc + dedupe),
  `LLMConsolidator` (LLM-driven cleanup), `EvolutionConsolidator` (5-stage
  pipeline), `ConsolidatorScheduler` (APScheduler-based runner), plus v7
  semantic graph scoring and cognitive sleep cleanup.
- **`graph/`** — entity/relation extraction and the read-side `KnowledgeGraph`.
- **`serve/`** — FastAPI app and helpers. `app.create_app()` wires ~40 routes
  to handlers in `handlers.py`. `watcher.py` polls the filesystem for new
  transcripts.
- **`cli/`** — `main.main(argv)` is a 1-line dispatcher; each subcommand lives
  in `cli/commands/` (read / write / serve / hooks / graph / cognitive). The
  positional `export <dir>` form writes a v7 bundle; the legacy `export
  --out <file> [--q <query>]` form remains available.
- **`mcp/`** — stdio MCP server exposing `recall`, `list_wiki`, `get_wiki`,
  `ask`, `inject`, memory write tools, semantic graph tools, and cognitive
  audit tools to the host LLM client.
- **`sdk.py` / `sdk_extensions.py`** — shared in-process and HTTP
  `MemoryClient` contract, namespaces, graph operations, cognitive sleep, and
  portable bundle helpers.
- **`export/`** — white-box `MEMORY.md` bundle export/import and Wiki forks.
- **`security/`** — local secret storage abstraction backed by
  `~/.loop_memory/secrets.json` with mode `0600`.
- **`examples/`** — `demo.py` end-to-end smoke test used by CI.

## The 5-stage evolution pipeline

`jobs.evolution.EvolutionConsolidator` runs the dashboard's main visual loop:

1. **Stage 1 — Signal-Aware Scoring**: blend importance with recall_count and
   feedback, so "what the user actually uses" floats up. When the recall
   query is **1–2 tokens** (`MemoryStore.recall()` `short_query` branch, since
   0.4.4) an extra `recall_count` weight nudges a memory the user has
   surfaced before over a substring-only match, capped at +30 percent.
   3+ token queries are unchanged. See `tests/test_short_query.py` (5 cases).
2. **Stage 2 — Semantic Batching**: greedy cosine clustering into ≤
   `CLUSTER_MAX` buckets (fallback: hashed embeddings).
3. **Stage 3 — Per-Cluster Distillation**: ask the LLM for a 1-sentence
   summary, refined importance, and a keep / drop / rewrite plan.
4. **Stage 4 — Hierarchical Wiki Synthesis**: roll cluster summaries into
   user-profile dimensions (preferences / decisions / projects / domain /
   feedback), merging with existing wiki pages by slug.
5. **Stage 5 — Evolution Memo**: persist what changed so the next run can
   re-prompt with the user's evolving preferences.

The rule-based synthesizer is the safety net: if the LLM is missing or
returns junk, the dashboard still shows real wiki content (with topic-aware
slugs and recorded evidence_ids for drill-down).

### Storage hot paths worth knowing

- `MemoryStore.get_signals(memory_ids)` — batched `memory_signals` fetch
  that replaced the per-id N+1 in the recall path. Called once per
  ranked result page; safe to pass any size (returns a `dict[memory_id]`,
  defaults to zeros for ids that have no row yet).
- `MemoryStore.top_signals(kind, limit)` — top-N by `recall_count` /
  `positive` / `negative`. The dashboard's "most recalled" widget and
  the wiki distillation prioritisation both call this.

### Cognitive sweep observability

`loop_memory/jobs/cognitive.py::cognitive_sleep` is the only job that
may run for minutes on a large store. Each stage writes its elapsed
ms into `CognitiveReportView.stages`; a deadline (`deadline_seconds`)
short-circuits with `aborted=True` + `abort_reason` naming the slow
stage. The HTTP body, the in-process SDK and the CLI all share the
same view — no second JSON shape to maintain.

## Request lifecycle

```mermaid
sequenceDiagram
    participant U as User CLI / Web UI
    participant S as FastAPI (serve/app.py)
    participant H as handlers
    participant DB as MemoryStore (SQLite)
    participant J as ConsolidatorScheduler

    U->>S: POST /api/admin/consolidate-now
    S->>J: scheduler.run_now(trigger="manual")
    J->>DB: start_pipeline_run
    J->>DB: rescore + cluster + distill
    J->>LLM: complete(...)
    J->>DB: write wiki pages, finish_pipeline_run
    J-->>S: result
    S-->>U: {queued: false, result: {...}}
```

## Configuration & secrets

- All persistent settings live in the SQLite `settings` table.
- API keys **never** land in that table — they're stored in the local
  `~/.loop_memory/secrets.json` file with mode `0600`, under a per-provider
  account name (`llm/<provider>/api_key`).
- The settings blob carries only `api_key_set: bool` + `api_key_account: str`
  as hints, so the UI can render a "key configured" badge without leaking
  the secret to disk.
- `validate_config()` is the single source of truth for clamping
  `temperature`, `max_output_tokens`, `batch_size`, etc.

## Testing

- **526 tests across 39 files** (all run via `pytest -q`), pinned by:
  - 21 new in 0.4.4 (`tests/test_cli_rules.py` + `tests/test_short_query.py`)
  - cognitive-sleep stages / abort + bulk `get_signals` covered in
    `tests/test_universal_memory.py` (e.g. `test_get_signals_*`,
    `test_cognitive_sleep_*`) and `tests/test_serve_handlers.py`
    (`test_returns_all_six_stages_even_when_empty`).
  - prior releases (`tests/test_universal_memory.py` etc.).
- New code should ship with at least one focused unit test in `tests/`.
- CI (`.github/workflows/tests.yml`) runs ruff + mypy (advisory) + pytest
  with a 60% coverage floor.
