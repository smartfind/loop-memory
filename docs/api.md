# HTTP API reference

Loop Memory exposes a FastAPI app on `127.0.0.1:7767` by default. The
interactive Swagger UI is served at **`/docs`** and the OpenAPI schema at
**`/openapi.json`** — this page is the canonical, hand-curated reference that
the UI consumes, so if you change a route, update this file in the same PR.

All routes are JSON-in / JSON-out unless noted. The default Content-Type is
`application/json`. Errors are returned as `{"detail": "..."}` with an
appropriate 4xx/5xx status.

## Conventions

- IDs are short strings (memory ids look like `m_3f9a2c1b`, session ids like
  `s_2026_07_18_…`). They are stable across the lifetime of the database.
- Timestamps are Unix epoch seconds (float).
- Scores are floats in `[0, 1]` unless otherwise noted.
- `POST` endpoints that mutate state return the updated object; `DELETE`
  returns `{"ok": true}`.

## Read surface

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Serves the Vue 3 single-page UI |
| GET | `/api/stats` | Aggregate counters (memories, sessions, wiki pages) |
| GET | `/api/insights` | Insights-dashboard payload (charts, decay distribution) |
| GET | `/api/weekly-report` | Auto-generated weekly digest |
| GET | `/api/source-health` | Per-source watcher status |
| GET | `/api/diag` | Diagnostic snapshot for bug reports |
| GET | `/api/pipeline` | Pipeline state per stage (scanned/dropped/wiki counts) |
| GET | `/api/pipeline/{stage}/items` | Items currently sitting in a given stage |
| GET | `/api/pipeline/score-distribution` | Histogram of memory scores |
| GET | `/api/pipeline/decay-stats` | Decay curve + per-bucket counts |
| GET | `/api/memories/{mid}/score` | Score breakdown for a single memory |
| GET | `/api/signals` | Active scoring signals (recall / feedback / negative) |
| GET | `/api/graph` | Knowledge-graph nodes + edges (used by the 3D view) |
| GET | `/api/graph/entity/{name}/memories` | Memories backing a graph entity |
| GET | `/api/sessions` | All conversation sessions, paginated |
| GET | `/api/sessions/counts` | Session counts grouped by source |
| GET | `/api/sessions/{session_id}/memories` | Memories of one session |
| GET | `/api/memories` | List memories (filter by `source`, `session_id`, `min_score`, `limit`, `offset`) |
| GET | `/api/recall` | Top-K recall for a query (`?q=…&k=10`) |
| GET | `/api/llm-audit` | Recent LLM calls + token usage |
| GET | `/api/write-guard` | Write-guard rail status |
| GET | `/api/wiki` | List wiki pages |
| GET | `/api/wiki/{page_id}` | Single wiki page (full body, evidence, tags) |
| GET | `/api/wiki/export` | Bulk export (`?format=markdown|json`) |
| GET | `/api/wiki/{page_id}/export` | Single-page export with context |
| GET | `/api/admin/llm/providers` | Registered provider specs |
| GET | `/api/admin/llm/config` | Saved LLM config (no secrets) |
| GET | `/api/admin/llm/status` | Live status: `api_key_set`, `last_test_ok`, `reachability`, … |
| GET | `/api/admin/llm/runs` | Recent distillation runs |

## Write surface

| Method | Path | Body | Purpose |
| --- | --- | --- | --- |
| POST | `/api/admin/ingest` | `{items:[…]}` | Bulk-ingest raw conversations (used by the watcher) |
| POST | `/api/admin/evolution/run` | `{dry_run?:bool, full?:bool}` | Trigger the 5-stage distillation now |
| POST | `/api/admin/rescore` | `{}` | Re-score every memory using the current model |
| POST | `/api/admin/gc` | `{older_than_days:int}` | Garbage-collect low-importance memories |
| POST | `/api/admin/consolidate` | `{}` | Legacy single-pass consolidator |
| POST | `/api/admin/consolidate-now` | `{}` | Same as above, force-run synchronously |
| POST | `/api/admin/bump-recall` | `{memory_id, delta?}` | Increase the recall counter on a memory (used by hooks) |
| POST | `/api/admin/graph/rebuild` | `{}` | Rebuild the knowledge graph from scratch |
| POST | `/api/memories/{mid}/feedback` | `{kind:"up"|"down", note?}` | User feedback signal |
| POST | `/api/contradictions/resolve` | `{a,b, action:"merge"|"keep_both"|"discard_a"|"discard_b"}` | Resolve a detected contradiction |
| PUT | `/api/admin/llm/config` | `{provider, model, behaviour?, schedule?}` | Save LLM config |
| POST | `/api/admin/llm/test` | `{provider, model?, api_key?}` | Connectivity test |
| POST | `/api/admin/llm/run` | `{mode?:"once"|"stage1"\|…}` | Trigger an ad-hoc run |
| POST | `/api/admin/llm/schedule` | `{mode, interval_minutes?, hour?, minute?, weekday?, after_ingest_idle_sec?}` | Update scheduler |
| DELETE | `/api/admin/llm/key` | — | Forget the saved key |
| POST | `/api/wiki` | `{slug,title,summary,body,tags,importance}` | Create a wiki page |
| PUT | `/api/wiki/{page_id}` | same | Update a wiki page |
| DELETE | `/api/wiki/{page_id}` | — | Delete a wiki page |
| POST | `/api/wiki/{page_id}/resummarize` | `{hint?}` | Ask the LLM to re-summarize a single page |
| POST | `/api/wiki/import` | `{format:"json"\|"markdown", pages?\|markdown?}` | Bulk import |
| POST | `/api/wiki/ask` | `{question, k?}` | Ask a question grounded in wiki content |
| DELETE | `/api/memories/{mid}` | — | Hard-delete a memory |
| DELETE | `/api/sessions/{sid}` | — | Hard-delete a session + its memories |

## Common response shapes

```jsonc
// /api/admin/llm/status
{
  "is_running": false,
  "next_run": 1721320800.0,
  "schedule": { "enabled": true, "mode": "daily", "hour": 3, "minute": 0 },
  "behaviour": {
    "batch_size": 50, "max_text_chars": 4000, "max_output_tokens": 4096,
    "temperature": 0.3, "enable_score": true, "enable_filter": true,
    "enable_summarize": true
  },
  "provider": "MiniMax",
  "model": "MiniMax-M2.7",
  "api_key_set": true,
  "api_key_fingerprint": "ChYM·da7ff5",
  "last_test_ok": true,
  "last_test_at": 1721319000.0,
  "progress": { "stage": "cluster", "pct": 0.42, "msg": "…" }
}

// /api/recall?q=…&k=10
{
  "query": "user prefers dark mode",
  "hits": [
    { "memory_id": "m_…", "score": 0.91, "snippet": "…", "wiki_page_id": null }
  ]
}
```

## Versioning & stability

- The `/api/*` namespace is considered stable for the `0.x` series. Breaking
  changes bump the path to `/api/v2/…` and ship with a deprecation alias.
- The non-`/api/` top-level route (`/`, `/docs`, `/openapi.json`) is the UI
  surface and may move at any time.

## Programmatic clients

- The MCP server (`loop_memory/mcp/`) implements a small subset of the above
  routes as MCP tools — see `docs/auto-capture.md` for how to wire it into
  Claude/Codex.
- The CLI (`loop-memory`) is a thin wrapper around the same routes — useful
  for cron jobs and shell pipelines.
