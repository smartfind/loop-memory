# Contributing to Loop Memory

Thanks for considering a contribution! Loop Memory is a small, dependency-free
core that grows through opt-in extras. The goal of this guide is to get a new
contributor from `git clone` to a merged PR in under an hour.

## Ground rules

- The core `loop_memory` package stays **dependency-free** at runtime. Optional
  integrations (LLM SDKs, vector stores, embedders) live under
  `[project.optional-dependencies]` in `pyproject.toml` and are pulled in via
  `pip install ".[openai]"` etc. Do **not** add a hard import for an optional
  dependency inside the core package.
- Public functions and HTTP handlers must keep **type hints** and a short
  **docstring**. If you change the schema of any `MemoryItem` / `WikiPage` /
  `EvolutionStats` field, update both the relevant doc and `tests/`.
- Every PR must pass `pytest -q` locally (see below).
- Keep PRs small and one-logical-change-per-commit. AI-generated code is
  welcome as long as a human has read it.

## Repository map

```
loop_memory/
├── cli/                # `loop-memory` console entry-point (typer-style)
├── serve/              # FastAPI app + static UI
│   ├── app.py          # all @app.get/@app.post routes live here
│   ├── handlers.py     # request/response helpers (no route decorators)
│   └── static/         # Vue 3 CDN ESM frontend — no build step
├── jobs/               # background work
│   ├── evolution.py    # 5-stage distillation pipeline (Stage 1→5)
│   ├── consolidate.py  # legacy single-pass consolidator
│   ├── llm_consolidate.py
│   └── scheduler.py    # cron / after-ingest / realtime scheduler
├── llm/                # LLM provider layer
│   ├── providers.py    # PROVIDERS dict + ProviderSpec + validation
│   ├── base.py         # LLMClient protocol
│   └── openai_adapter.py
├── memory/             # dataclasses (MemoryItem, WikiPage, Entity, …)
├── graph/              # knowledge-graph extract + build
├── storage/            # SQLite-backed MemoryStore
├── security/           # keychain / local-secret store
└── mcp/                # optional Model-Context-Protocol server
tests/                  # pytest, mirror-tree under loop_memory/
docs/                   # architecture, auto-capture, API, providers
```

## Local dev loop

```bash
git clone https://github.com/<you>/loop-memory.git
cd loop-memory
python3.10 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,serve,openai]"
pytest -q
```

Then launch the server (the production launchd plist is `~/Library/LaunchAgents/com.loopmemory.server.plist`):

```bash
python -m loop_memory.cli.main serve --port 7767
# or, to develop the frontend with hot-edit:
python -m loop_memory.cli.main serve --reload
```

The dev UI is served at <http://127.0.0.1:7767/>. The interactive OpenAPI
document is at <http://127.0.0.1:7767/docs>.

## Where secrets live

API keys are **never** stored in SQLite or in the settings blob. They go to:

- macOS: the user-login Keychain via `loop_memory/security/secrets.py`.
- Linux/headless: `~/.loop_memory/secrets.json` (mode 0600), with the OS keychain
  as a fallback when present.

If you add a new secret, store it through `security.get_secret(name)` /
`set_secret(name, value)` so the same fallback chain is used everywhere.

## Adding a new LLM provider

LLM providers are registered in a single dict — no plugin loader required.

1. Open `loop_memory/llm/providers.py` and add an entry to `PROVIDERS`:

   ```python
   PROVIDERS["my-co"] = ProviderSpec(
       label="MyCo",
       default_model="myco-3-mini",
       default_base_url="https://api.myco.example/v1",
       needs_api_key=True,
       adapter="openai_compat",   # reuse OpenAI-compatible adapter
       notes="OpenAI-compatible chat-completions endpoint.",
   )
   ```

2. If the wire format is **not** OpenAI-compatible, add a dedicated adapter in
   `loop_memory/llm/<myco>_adapter.py` implementing the `LLMClient` protocol
   (see `base.py`), and set `adapter="myco"` on the spec.
3. Add tests in `tests/test_llm_myco.py` — mock the HTTP layer; do **not** call
   the real provider in CI.
4. Update the docs/providers table in `docs/providers.md`.
5. Open a PR — the "Test provider" button in **Settings → Models** will pick
   up the new entry automatically.

## Adding a new ingestion source

Each source (Codex, Claude, Hermes, OpenClaw / clawx) is a small module in
`loop_memory/serve/watcher.py` (or a sub-module it imports). To add a new one:

1. Implement a `Watcher` class with `start()`, `stop()`, and a `poll_once()`
   method that emits `IngestItem` records.
2. Wire it into the CLI: `loop-memory hook --source <new> --watch <path>`.
3. Document the expected transcript layout in `docs/auto-capture.md`.
4. Add a fixture under `tests/fixtures/<source>/` and a test that ingests it.

## Touching the distillation pipeline

The 5-stage pipeline lives in `loop_memory/jobs/evolution.py`:

| Stage | Where | Purpose |
| --- | --- | --- |
| 1 | `EvolutionEngine._rescore()` | Signal-aware re-scoring (importance × recall × feedback) |
| 2 | `EvolutionEngine._cluster()` | Semantic batching into clusters |
| 3 | `EvolutionEngine._distill()` | Per-cluster atomic-fact distillation (prompt: `_CLUSTER_SYSTEM`) |
| 4 | `EvolutionEngine._synthesize_wiki()` | Hierarchical wiki page build (prompt: `_WIKI_SYSTEM`) |
| 5 | `EvolutionEngine._evolution_memo()` | Cross-page synthesis + contradiction notes |

Two non-negotiable policies from `v2`:

- **Completeness over compactness.** The distillation prompts (`_CLUSTER_SYSTEM`
  / `_WIKI_SYSTEM`) and the call-site `max_tokens` must never reintroduce hard
  character caps. If a bullet is incomplete, the fix is a better prompt, not a
  smaller cap.
- **No truncation mid-fact.** Bullet points are atomic; do not "save space" by
  stripping numbers, names, or constraints.

When you change a prompt, also update the matching snapshot test under
`tests/test_evolution_prompts.py` (if present) and add a new entry to
`CHANGELOG.md`.

## Tests

```bash
pytest -q                          # fast suite, no network
pytest -q -m "network"            # only tests that hit real APIs (skipped without keys)
pytest -q tests/test_evolution.py  # one module
```

Tests must not write to the user's real `~/.loop_memory/loop_memory.db` —
every test either uses an isolated `tmp_path` fixture or injects an in-memory
store.

## Commit & PR style

- One logical change per commit.
- Commit subject: `feat: …`, `fix: …`, `docs: …`, `chore: …`, `test: …`,
  `refactor: …`. Keep it ≤ 72 chars.
- Reference any issue number in the commit body (`Refs #123`).
- PR title mirrors the commit subject; PR body explains **why**, not just
  **what**, and includes screenshots for any UI change.

## Reporting a vulnerability

Please do **not** open a public issue. Follow `SECURITY.md`.
