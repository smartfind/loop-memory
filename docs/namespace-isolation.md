# Namespace isolation — who may read which memory, and who may write it

> **Status:** shipped in **0.4.22** (2026-11-15).
> Audit: `docs/research/2026-11-15.md`.

Loop Memory stores every memory under an optional `(agent_id, user_id)`
pair. Those two columns are the *namespace*: the row belongs to an
agent, to a user, or — when both are `NULL` — to nobody in particular,
which makes it **global**.

This page is the contract for how a namespace is applied to a read.

---

## The rule

> **The namespace predicate lives in the SQL, not in the caller.**

A caller that asks for a namespace gets a result set that was
*selected* by that namespace, not a result set that was *filtered*
after the selection. Concretely, the `LIMIT` is applied to the
scoped rows.

| Caller asks for | A row is visible when |
| --- | --- |
| nothing (no namespace) | always — legacy see-everything behaviour |
| `agent_id = "a1"` | `row.agent_id IS NULL` (global) **or** `row.agent_id = "a1"` |
| `user_id = "u1"` | `row.user_id IS NULL` (global) **or** `row.user_id = "u1"` |
| both | both predicates hold |

The SQL fragment lives in one place,
`loop_memory/storage/sqlite_store.py::_scope_clause`, and every
retrieval channel builds its query from it.

### Why "global rows stay visible"

A memory written by the ingest pipeline carries no `agent_id` — it
describes *the user's work*, not one agent's view of it. The
watcher, the consolidator and the dream job all write global rows.
Hiding them from every scoped caller would make the memory useless:
the agent would see only what it wrote itself.

This is also the semantics the SDK has always documented: *"Memories
with no `agent_id` set are treated as global and visible to every
caller."*

---

## What shipped in 0.4.22

The weekly audit for this release found that the namespace filter
**did not work**, in two different ways.

### 1. On the legacy path it was a silent no-op

`GET /api/v1/recall?agent_id=X` was documented as *"Unified recall
filtered to a single Agent namespace"*. But the filter was applied to
the store's **output**, and the legacy `MemoryStore.recall()` never
returned the `agent_id` / `user_id` columns. The route's predicate
read `None`/`None` for every row, decided every row was "global", and
let all of them through.

**An agent that explicitly asked for its own namespace was handed
every other agent's memories as well.**

### 2. On the hybrid path it ran after the `LIMIT`

`MemoryStore.recall_hybrid()` fuses three channels (BM25, semantic,
entity) and trims the result to `limit`. The filter ran afterwards,
on the trimmed list. A foreign namespace could therefore spend the
caller's *entire* result budget: an agent asking for 5 hits could get
**zero**, while its own matching rows sat further down the ranking.

### The fix

| Surface | Change |
| --- | --- |
| `storage/sqlite_store.py` | new `_scope_clause()` builder; `recall()`, `recall_hybrid()` and `recall_as_of()` take `agent_id` / `user_id` and apply the predicate **in SQL** |
| `storage/retrieval.py` | `bm25_search()` takes `agent_id` / `user_id` and applies the predicate to **both** the FTS5 query and the `LIKE` backstop |
| `search_by_embedding()` | semantic channel scoped in SQL |
| `search_memories_by_entity_names()` | entity channel scoped in SQL |
| `recall()` output | now carries `agent_id` / `user_id` on every hit, so a namespace is visible to the caller and a filter applied on top of the payload can tell the rows apart |
| `serve/routes/memories.py` | `/api/v1/recall` passes the namespace into the store instead of filtering its output |
| `serve/routes/system.py` | `/api/recall` accepts `agent_id` / `user_id` |
| `mcp/__init__.py` | the `recall` tool scopes to `LOOP_MEMORY_AGENT_ID` / `LOOP_MEMORY_USER_ID`, and accepts explicit overrides |
| `sdk.py` | a client bound to a namespace **reads** from it, not just writes into it |
| `cli/commands/read.py` | `loop-memory recall --agent-id <id> --user-id <id>` |

Every retrieval channel is scoped, so the fused ranking — and the
`limit` that trims it — only ever sees the caller's own rows.

### Behaviour change (read this one)

`MemoryClient.memory(store, agent_id="x")` and
`MemoryClient.http(url, agent_id="x")` now **read** from the `x`
namespace, not just write into it. Before 0.4.22 they wrote into
`x` and read from everywhere, which is exactly the asymmetry that
hid the leak.

If you relied on the old see-everything behaviour, pass an explicit
empty string:

```python
client.recall("deploy process", agent_id="")   # no restriction
```

For the HTTP surface, simply omit the parameter.

---

## Using it

### HTTP

```bash
# only what agent "codex" wrote (plus global memories)
curl 'http://127.0.0.1:7767/api/v1/recall?q=deploy&agent_id=codex'

# everything (legacy behaviour)
curl 'http://127.0.0.1:7767/api/v1/recall?q=deploy'
```

### SDK

```python
from loop_memory.sdk import MemoryClient

client = MemoryClient.http("http://127.0.0.1:7767", agent_id="codex")

client.remember("the deploy pipeline uses blue/green")   # written as "codex"
hits = client.recall("deploy pipeline")                   # read as "codex"

hits = client.recall("deploy pipeline", agent_id="")      # escape hatch: no restriction
```

### MCP

The MCP server is started once per client, so the environment already
identifies the caller:

```bash
LOOP_MEMORY_AGENT_ID=codex loop-memory mcp
```

The `recall` tool then answers from that namespace. The tool also
accepts explicit `agent_id` / `user_id` arguments, which override the
environment; an empty string means "no restriction".

### CLI

```bash
loop-memory recall "deploy pipeline" --agent-id codex
loop-memory recall "deploy pipeline" --user-id alice
```

---

## The one deliberate loose end

An **explicitly empty** namespace (`agent_id=""`) means "no
restriction" rather than "match nothing".

This is a conscious choice. Upstream projects that added hard
tenancy this week went the other way: `plastic-labs/honcho` v3.6.0
refuses to give a tenant an empty namespace, and
`thedotmack/claude-mem` made *"an explicitly empty project selection
show nothing instead of observations from every project"* (#5866).

Loop Memory's `agent_id` is an *optional narrowing filter* on a
single-user store, not a hard multi-tenant boundary, so the escape
hatch is kept — but it is explicit, documented, and never the
default. A future strict mode (fail-closed on an empty namespace) is
the obvious next step if Loop Memory grows into a shared deployment.

---

## What this does **not** change

`GET /api/v1/memories` (the list endpoint) keeps its **exact-match**
semantics: `agent_id=x` returns only the rows whose `agent_id` is
exactly `x`, and global rows are excluded. That endpoint has always
behaved that way and 0.4.22 leaves it alone.

Recall is deliberately more forgiving, because the global rows are
the ones the ingest pipeline writes.

---

## The write side (audit 2026-11-22, loop-memory 0.4.23)

0.4.22 made the namespace a **read** guarantee. 0.4.23 closes the
other half: a namespace is a **write** guarantee too.

### The rule

> A write touches **only** the rows of its own namespace.

The namespace of a memory is the pair `(agent_id, user_id)`, compared
with SQLite's null-safe `IS` operator:

| `agent_id` | `user_id` | namespace |
| --- | --- | --- |
| `NULL` | `NULL` | **global** — shared by every caller |
| `"codex"` | `NULL` | the `codex` agent, no user |
| `"codex"` | `"alice"` | the `codex` agent acting for `alice` |

The global namespace is a namespace like any other. An agent-scoped
write does **not** reach into it.

### What was broken

`MemoryStore.upsert_memory()` matched the namespace with

```sql
((agent_id IS ? AND user_id IS ?)              -- the caller's own rows
 OR (agent_id IS NULL AND user_id IS NULL AND ? IS NULL))  -- the global rows
```

The second branch was gated on the **user_id** parameter, so it
fired for every caller that had an agent but no user — the common
case. Two consequences, both reproduced before the fix:

1. **An agent write superseded a global row of the same slot.** The
   shared fact disappeared from every other caller's recall and was
   replaced by one agent's private value.
2. **An agent write collapsed onto a global row with the same text
   and overwrote it**, re-labelling the shared row with the agent's
   own `agent_id`. Every other caller lost the memory.

Both are now impossible: the namespace is matched *exactly*, one
`(agent_id, user_id)` pair.

### A slot has a clock (fact time, not arrival time)

A logical fact slot (`subject_key`) keeps **one** live version. The
live version used to be "whatever was written last", which meant a
fact that arrived late but happened *earlier* could take the slot
away from the current conclusion — replaying an old transcript made
the store answer with the stale value.

The slot is now ordered by **fact time**:

```
fact_time = COALESCE(valid_at, created_at)
```

* `created_at` is when the fact became true (or, when the caller did
  not say, when it was written);
* `valid_at` is when the fact starts being true, and wins over
  `created_at` when both are present.

A write whose fact time is **older** than the slot's live version
does not take the slot. It is recorded as the historical version
instead: the pointer still reads forward (old → new), the row is
never deleted, and the newer conclusion keeps answering the query.

The read side agrees: `_filter_latest_only()` picks the winner of a
slot by the same fact time, so the write path and the read path
cannot disagree about which version of a fact is current.

### The fact time has to survive the surface

`POST /api/v1/memories` used to parse `created_at`, validate it, and
then **drop it** — the row was stamped with "now". The batch route
and the SDK already honoured it, so the two surfaces of the same
operation disagreed.

| Surface | `created_at` | `subject_key` |
| --- | --- | --- |
| `POST /api/v1/memories` | honoured since 0.4.23 | since 0.4.16 |
| `POST /api/v1/memories:batch` | since 0.4.15 | since 0.4.23 |
| `MemoryClient.remember(...)` (in-process) | since 0.4.16 | since 0.4.16 |
| `MemoryClient.remember(...)` (HTTP) | since 0.4.23 | since 0.4.16 |

### `external_id` is idempotent everywhere

The unique index is `(agent_id, user_id, external_id)`, but SQLite
treats NULLs as distinct inside a unique index — so two global rows
with the same `external_id` are perfectly legal. The re-route was
also guarded by `agent_id is not None`, so a global caller never
re-routed at all and every re-push created another row.

The re-route now runs for **every** namespace, matched exactly, so
re-pushing the same `(agent_id, user_id, external_id)` updates the row
in place — in the global namespace too.

---

## Upstream pattern references

Patterns only — no code was copied.

| Project | Release | What was borrowed |
| --- | --- | --- |
| `plastic-labs/honcho` | v3.6.0 (2026-11-10, AGPL-3.0) | `tenant_id` leading every primary key; the predicate scoped **every read and write**; refusing to hand out an empty namespace |
| `EverMind-AI/EverOS` | v1.6.0 / v1.7.0 (2026-11-11) | owner-scoped deletion; `503 SESSION_BUSY` instead of a half-done delete |
| `thedotmack/claude-mem` | v15.16.x → v15.18.4 (2026-11-11 → 11-15) | "each eve scope gets its own claude-mem project by default"; per-host hook identity; bounded listing offsets |
| `vectorize-io/hindsight` | v1.2.1 (2026-11-10) | #6280 — a bank clone can no longer take over another bank's alias |
| `rohitg00/agentmemory` | v0.9.33 (2026-11-09) | "enrichment scoped to the current project" (#1220); "scoped reflection stays in its project" (#1544) |
| `TencentCloud/TencentDB-Agent-Memory` | v2.0.4 (2026-11-19) | "整理按对话发生的时间排序，后到的更早事实不再覆盖较新的结论" — order the consolidation by when the conversation happened, so a later-arriving earlier fact no longer overwrites a newer conclusion |
| `thedotmack/claude-mem` | v15.19.0 (2026-11-16) | #6105 — the MCP `save_memory` tool honours the same exclusion rule the HTTP route already honoured: one operation must enforce one rule on every surface |

---

## See also

- `docs/api.md` — the HTTP surface
- `docs/architecture.md` — where the predicate sits in the retrieval pipeline
- `docs/research/2026-11-15.md` — the weekly audit the read-side release came from
- `docs/research/2026-11-22.md` — the weekly audit the write-side release came from
