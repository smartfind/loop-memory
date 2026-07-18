## [Unreleased]

### Distillation policy v2 — completeness over compactness
- **Stage 3 (`_CLUSTER_SYSTEM`) and Stage 4 (`_WIKI_SYSTEM`) prompts no
  longer hard-cap title / summary / body / distill lengths.** The previous
  `<= 60 / 180 / 200 chars` and `3-8 bullets` limits were truncating
  distillates mid-fact. The new prompt explicitly tells the LLM that
  "losing a fact is much worse than a longer page" and to surface every
  decision, number, name, constraint, error, and workaround in at least
  one bullet.
- **Default token budget raised**: `max_text_chars` 1200 → 4000,
  `max_output_tokens` 800 → 4096, validator ceiling 4096 → 8192. Per-call
  cap on stage 3 distillation raised from 900 → 4096 tokens; legacy
  consolidator call sites raised from 800 → 4096.
- **Wiki card rendering**: `bulletsOf()` no longer slices the first 6
  bullets; the card now shows every atomic bullet the LLM produced so
  freshly-distilled pages no longer look truncated in the grid. The
  dead `bodyPreview()` helper was removed.

### Docs (open-source release prep)
- `CONTRIBUTING.md` rewritten: pytest instead of unittest, accurate
  subpackage layout, secrets-storage guidance, the new LLM-provider
  registration procedure, and the distillation-policy "completeness
  over compactness" rule.
- New `docs/api.md`: canonical, hand-curated HTTP API reference
  covering every route the UI consumes (read + write surface).
- New `docs/providers.md`: built-in providers table, default models,
  base URLs, token-limit guidance, and the API-key storage story.
- `README.md` now links the docs in a single table so newcomers can
  find everything without grepping the repo.

## [0.3.0] — 2026-07-11

### Wiki fallback (v0.3.0)
- The 5-stage evolution consolidator's wiki step now falls back to a
  rule-based synthesizer when the configured LLM is unreachable or
  returns junk / 0 pages. The user always sees real wiki content
  after a run, even with a bad/missing API key.
- The rule-based synthesizer now produces meaningful slugs from
  the dominant `kind` + extracted topic words (e.g.
  `episode-outcome-i-see-one`, `fact-请立即生成a股早盘…`) instead of
  opaque `auto-cluster-<hash>` blobs, and records the actual
  evidence_ids so the dashboard drill-down can link back to
  the contributing memories.

### Pipeline evidence (v0.3.0)
- Stage 3 (distill) summaries now carry `evidence_ids`,
  `dominating_tag`, `kind`, and `avg_importance` so stage 4 has
  the metadata it needs to build rich wiki pages from them.


### UI fixes (v0.3.0)
- **Run progress strip is back**: it had been hidden by default
  (`data-hidden="true"` in HTML) so users only saw it during an
  active run, then lost track of when the next one was scheduled.
  Now visible by default below the topbar with the next-run time,
  and turns into a live "AI consolidating memories… N/M" bar with
  percentage during a run.
- **Score-distribution X-axis labels visible**: `0.0, 0.1, ... 0.9`
  were rendered at `y=h` and clipped by the SVG viewBox. Moved to
  `y=h-4` and bumped viewBox from 140→160 so labels fit.
- **Time-decay bucket labels visible**: `<1d, 1-7d, 7-30d, 30-90d,
  >90d` now render with the same fix.
- **Chart cards** now have a subtle accent-tinted background and a
  visible axis line so the data has a frame.
- **Chart label CSS**: `.bin-label` and `.bucket-label` had no rule
  before — now `10px monospace centered` so all axis text reads
  cleanly.


### UI polish (v0.3.0)
- Dashboard topbar: removed the bulky "Next AI run" banner. Status is now
  a compact pill (idle / running / error) next to the brand.
- KPI cards: subtle gradient surface + 3px accent stripe + visible sparklines.
- Memory data flow: redesigned as a clean horizontal pipeline with 5 evenly
  spaced stage nodes (no more hand-drawn curves). Particles always flow
  along the path.
- Wiki cards: snippet peek + evidence count chip + cleaner titles (when
  LLM is configured).
- Knowledge graph: default cap of 120 entities (was 400) so labels stay
  readable.
- Dashboard "Active Stage" card and topbar pill now show a red error
  state when the configured LLM is unreachable, so users notice the
  silent-failure problem.

### Fixed
- Evolution consolidator was using the wrong settings key
  (`store.get_setting("llm", ...)` instead of `"llm_consolidator"`),
  so even with a configured provider it was always falling back to
  the rule-based path. Now properly pulls the LLM config.
- Rule-based wiki titles: strip `User intent:` / `[cron:...]` /
  environment-context prefixes so titles read as real topics.


### Fixed
- Dashboard tab now initialises: 5 KPI cards, animated data flow, score
  distribution and decay charts populate on tab open. Previously the tab
  switched but `initDashboard()` was missing — so all cards showed "…" forever.
- Flow-canvas action buttons (⚡ Run evolution / Rescore / Refresh) are now wired.
- Switching away from the Dashboard tab stops the polling loop to save CPU.

### Added
- `POST /api/admin/consolidate-now` — trigger the configured scheduler on demand.
- `GET /api/wiki/export` — dump all distilled wiki pages as one markdown string.
- `GET /api/wiki/{id}/export` — single-page context-block export.
- `POST /api/wiki/ask?q=...` — keyword-recall over wiki pages with paste-ready context.
- New CLI: `loop-memory consolidate-now`, `loop-memory export`, `loop-memory ask`.
- Dashboard **⚡ Run now** button next to **AI Run** — calls the scheduler.
- Wiki tab **⇩ Export**, **🔎 Ask**, **ⓘ How to use** toolbar buttons.
- Per-card **⎘** copy button — copies a wiki page as context for any LLM client.
- "How to use distilled knowledge" modal with the three delivery patterns.

### Fixed
- Dashboard score/distill/wiki drill-downs now include real `evidence_ids`
  (so clicking a stage shows the actual memories and wiki pages touched).
- Pipeline `ingest` stage now records its evidence list.

# Changelog

All notable changes to Loop Memory will be documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **LLM-driven consolidator** with a settings drawer in the UI. The
  loop_memory store can now be tidied up by an LLM (OpenAI / Anthropic /
  Ollama / any OpenAI-compatible HTTP endpoint) on a schedule or in
  real time after each ingest.
- New `settings` and `consolidation_runs` tables; bump schema to v3.
- New `loop_memory.llm.providers` module: `OpenAICompatProvider`,
  `AnthropicProvider`, `OllamaProvider`, and a zero-dependency
  `RuleBasedProvider` fallback (used when no LLM is configured).
- `LLMConsolidator` (`loop_memory.jobs.llm_consolidate`) — runs a
  three-pass pipeline over memories: rule-based pre-filter, LLM
  re-scoring, and LLM-driven distillation / merge.
- `ConsolidatorScheduler` (`loop_memory.jobs.scheduler`) — in-process
  thread that fires consolidations on `off / realtime / hourly /
  daily / weekly / interval` schedules. Hot-reloads when the user
  updates settings.
- New admin endpoints:
  - `GET  /api/admin/llm/providers` — list of supported providers
  - `GET  /api/admin/llm/config` / `PUT /api/admin/llm/config`
  - `POST /api/admin/llm/test` — smoke-test a provider
  - `GET  /api/admin/llm/status` — scheduler state
  - `GET  /api/admin/llm/runs` — last 20 run records
  - `POST /api/admin/llm/run` — run a pass right now (with `dry_run`)
  - `POST /api/admin/llm/schedule` — quick-toggle the schedule
- New "Settings" + "AI Consolidate" buttons in the header.
- LLM call responses are cached for 5 minutes (batch hash + provider
  model + temperature) so manual reruns don't burn duplicate tokens.
- 19 new unit tests covering noise heuristics, JSON extraction, the
  full provider config pipeline, and the end-to-end consolidator.
- **Auto-feedback into every LLM client** (Codex / Claude Code /
  Hermes / OpenClaw) via three zero-dep CLI commands:
  - `loop-memory install-hooks` — auto-writes MCP + SessionStart
    hook configs into `~/.codex/config.toml`,
    `~/.claude/{mcp.json,settings.json}` and `~/.hermes/mcp.json`.
    Idempotent TOML/JSON upsert, never duplicates.
  - `loop-memory inject [query]` — emits a `# Long-term memory
    context` markdown block (distilled wiki + recent relevant
    memories) for SessionStart hooks.
  - `loop-memory mcp` — stdio JSON-RPC 2.0 MCP server with five
    tools: `recall`, `list_wiki`, `get_wiki`, `recent_memories`,
    `wiki_summary`. Launched per-client, no third-party deps.
- 15 new tests covering the MCP server dispatch, install-hooks
  idempotency, and the inject output format.
- **Scoring v2** (`MemoryStore.score_components`) — score is now a
  weighted blend of importance + recency + usage + feedback instead
  of just importance × recency. Usage is `log1p(recall_count) /
  log1p(100)` weighted by recency of last recall; feedback is
  `tanh((positive - negative) / 3)` and sticky (no time decay).
  `rescore_all` reuses the same formula across the whole DB.
- **New APIs**: `GET /api/memories/{id}/score`, `GET /api/pipeline/
  score-distribution`, `GET /api/pipeline/decay-stats`, `POST
  /api/admin/bump-recall?ids=...` (query-param shape so it works
  under FastAPI 0.139's stricter body validation).
- **Dashboard v2** — 5 KPI cards with live sparklines, an animated
  particle flow on the SVG pipeline, a real score-distribution
  histogram and a time-decay chart. Per-memory "why?" expands the
  score breakdown (importance / recency / usage / feedback). New
  ↻ button bumps recall_count from the UI.
- 11 new tests covering v2 score math, rescore_all idempotency,
  score-distribution shape, decay-bucket math (103 → 114).


- **OpenClaw / clawx real-format ingest** — the previous loader only
  accepted a flat `{role, content, ts}` JSONL shape, which is not what
  clawx writes. Rewrote `OpenClawLoader` to handle the actual session
  format (`type=session`, `type=message`, with `message.content` as
  an array of typed parts: text / thinking / toolCall / toolResult).
  Now picks up 99 real session jsonl files + 9 markdown daily logs
  from `~/.openclaw/agents/main/sessions` and
  `~/.openclaw/workspace/memory`, instead of zero.
- Companion files (`*.trajectory.jsonl`, `*.checkpoint.*.jsonl`,
  `*.trajectory-path.json`, `sessions.json`) are skipped to avoid
  double-counting.
- `discover()` now whitelists real session directories and skips
  vendor dirs (`node_modules`, `dist`, `build`, `.git`).
- 5 new tests covering parse / discover / markdown / vendor-skip /
  legacy-shape (98 → 103 tests).


- **Dashboard tab + 5-stage Evolution Consolidator**:
  - New tab with 4 KPI cards, animated data-flow of the pipeline,
    drill-down panel (click any node to see what flowed through), and
    per-memory 👍/👎 feedback buttons.
  - `EvolutionConsolidator` (`loop_memory.jobs.evolution`) replaces
    the old single-pass consolidator with: signal-aware scoring,
    semantic clustering, per-cluster LLM distillation, hierarchical
    wiki synthesis (preferences/decisions/projects/domain/feedback),
    and an evolution memo persisted across runs.
  - Schema bumped to v5: new `memory_signals` and `pipeline_runs`
    tables.
  - New endpoints:
    - `GET  /api/pipeline` — live dashboard data
    - `GET  /api/pipeline/{stage}/items` — drill-down
    - `POST /api/memories/{id}/feedback?value=up|down|reset`
    - `GET  /api/signals?kind=recall_count|positive|negative`
    - `POST /api/admin/evolution/run` — run the 5-stage pipeline
  - 5 new tests (98 → 103 once the next batch lands, currently 98
    passing).


## [0.2.0] — 2026-07-10

### Added
- **SQLite-backed `MemoryStore`** — sessions + memories tables, WAL,
  full-text and embedding search, configurable retention.
- **Auto-ingest** for `codex`, `claude`, `hermes` local transcripts.
- **Time-weighted scoring** — `score = 0.35 · importance + 0.65 · ½^(age / half_life)`,
  surfaced as a `score` column and a UI percentage.
- **Background consolidation job** — single `Consolidator` that rescores,
  GCs TTL-expired memories, and merges cosine-near-duplicate ones.
- **Filesystem watcher** (`loop-memory hook`) — auto-imports new transcripts
  as soon as the producing tool finishes writing them.
- **Local web UI** (FastAPI + uvicorn) at `loop-memory serve --port 7676`.
  Timeline / search / source filter / time range / min-score filter /
  one-click ingest/consolidate/rescore.
- `loop-memory ingest codex|claude|hermes [path]` CLI command.
- 26 unit tests covering the core loop, storage, ingest, reflection,
  vector store, scoring and web API surface.

## [0.1.0] — 2026-07-10

### Added
- Initial public release.
- `Retrieve → Generate → Reflect → Store` loop engine.
- Four-tier memory: `ShortTermMemory`, `LongTermMemory`, `EpisodicMemory`, `ProceduralMemory`.
- Zero-dependency defaults: `EchoLLM`, `HashingEmbedder`, `IdentityEmbedder`.
- Optional `OpenAIClient` adapter behind the `openai` extra.
- Tiny CLI (`loop-memory chat`).

### Fixed (this session)
- **Dashboard tab empty data regression**: `Dashboard.refresh()` was
  calling a non-existent `/api/signals` endpoint, which made the
  `Promise.all` reject and aborted the whole refresh. Added the
  backend endpoint, and made the JS tolerate both array and
  `{items:[]}` response shapes.
- **Settings drawer: API key chip showed "saved —" placeholder** when
  the key was set via the secrets file directly (not via the
  `PUT /config` endpoint). The endpoint now derives `fingerprint` and
  `saved_at` from the on-disk secret + file mtime, so the chip always
  shows real metadata.
- **Dark mode `<select>` dropdowns** rendered with native white
  popups on Chromium. Added explicit dark `option` background +
  forced `color-scheme: dark` for selects in dark theme.

### Added (this session)
- **`GET /api/sessions/counts`** — per-source session/turn counts.
- **Sidebar source filter** now shows live session counts next to
  each option (e.g. `codex · 21`, `openclaw · 107`) so users can
  see at a glance which sources have data.

### v0.3.1 — recall + onboarding

#### Recall is now unified across memories + wiki + entities

The previous `loop-memory recall`, `loop-memory ask` and
`loop-memory inject` (SessionStart) all used a naive `text LIKE %q%`
search that:

* missed anything not literally present in the stored text,
* did **not** match Chinese / Japanese / Korean (CJK) tokens at all
  (`LIKE '%知识图谱%'` requires the substring to exist),
* never searched wiki pages or entities.

In v0.3.1 the new `store.recall()` runs a single ranked pipeline
across all three:

1. Tokenises the query into English words **and CJK bi-grams** (so
   `知识图谱` → `[知识, 识图, 图谱, 知识图, 识图谱]`).
2. Searches `memories.text`, `memories.tags`, `wiki_pages.title/body/
   summary/tags`, and `entities.name` with OR'd LIKE clauses.
3. Scores by token hits × importance × recency × usage signal.
4. Returns ranked lists `{memories, wiki, entities, tokens}`.
5. Bumps `memory_signals.recall_count` on surfaced memories so the
   dashboard's "Most recalled" widget stays accurate.

All four surfaces now use it:

* `loop-memory recall "..."` → ranked wiki + memories + entities
* `loop-memory ask "..."` → paste-ready context block for any LLM
* `loop-memory inject [query]` → SessionStart hook. With a query arg
  (the user's first message) it returns the most relevant wiki +
  memories for that query. Without a query it surfaces the user's
  top preference facts first, then the highest-importance wiki
  pages.
* MCP `recall` tool → returns all three ranked lists, with the
  wiki pages rendered as a distilled-knowledge section.

#### `/api/recall` + Timeline pane

* New `GET /api/recall?query=...&limit=N&include=memories,wiki,entities`
  endpoint on the web server.
* The Timeline pane's search box now uses `/api/recall` when the
  user types a query, and renders a "📚 Wiki" + "🔗 Entities" ribbon
  above the matched memories (chips click through to the Wiki tab).

#### Lower the open-source onboarding barrier

Three new CLI commands + one web UI panel:

* `loop-memory doctor` — green/red diagnostic screen. Surfaces
  what's installed, what's wired, what's broken, and gives
  copy-pasteable fix commands for every red dot.
* `loop-memory status` — concise one-screen summary.
* `loop-memory openclaw-setup` — installs a launchd plist that
  watches `~/.openclaw/agents/main/sessions` (clawx) **and**
  `~/.openclaw/workspace/memory` (daily logs) and auto-ingests
  finished transcripts.

The web UI kebab menu (⋮) now has a "🔍 Run doctor" item
(shortcut ⌘D) that opens a modal with the same diagnostic
information, rendered with green/yellow/red dots and inline fix
commands.

#### Misc fixes

* `loop-memory hook` now accepts multiple `--watch <path>` flags so
  a single watcher can cover multiple directories (used by
  `openclaw-setup` to cover both clawx transcripts and daily logs).
* `DEFAULT_DB` import-time snapshot bug fixed: CLI commands now use
  `default_db_path()` so tests (and embedded hosts) can change
  `$LOOP_MEMORY_DB` mid-process and have the next call see the new
  path. Resolves the long-standing `test_inject_emits_wiki_block`
  and `test_cli_ask_prints_block` flakes.
