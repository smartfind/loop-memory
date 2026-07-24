# Universal Agent Memory API

Loop Memory started life as a transcript-capture-and-distil tool for
Codex, Claude Code, Hermes, and OpenClaw. The **Universal Agent
Memory API** extends that foundation so *any* Agent — a LangChain
chain, an AutoGPT loop, a custom internal bot, a future client we
haven't heard of — can use the same long-term memory as the
supported CLIs.

The contract is intentionally small, protocol-agnostic, and
idempotent. It comes in three shapes so the Agent picks whatever
fits the runtime:

| Surface            | Where it lives                              | Use it when…                                  |
| ------------------ | ------------------------------------------- | --------------------------------------------- |
| **Python SDK**     | `loop_memory.sdk.MemoryClient`              | your Agent runs in the same Python process    |
| **HTTP / JSON**    | `POST /api/v1/memories` (and friends)       | your Agent is in another language / process   |
| **MCP tools**      | `loop-memory mcp` → memory, graph, and cognitive tools | your Agent already speaks MCP                |

All three are backed by the same SQLite store at
`~/.loop_memory/loop_memory.db` (overridable with `LOOP_MEMORY_DB`)
and the same scoring / recall pipeline (`recall_hybrid`). Writes are
keyed on the `(agent_id, user_id, external_id)` triple so retries are
safe; reads are filtered by the same triple so a noisy Agent can't
trample on another Agent's namespace.

## The 4-verb contract

| Verb        | What it does                                                       |
| ----------- | ------------------------------------------------------------------ |
| `remember`  | Upsert one (or many) memories. Idempotent on `external_id`.        |
| `recall`    | Unified search over the user's memories + wiki + entities.         |
| `feedback`  | Send 👍/👎/`ignore` on a memory. Improves ranking.                 |
| `forget`    | Hard-delete a memory by id or by its external triple.              |

## Identity model

Every memory carries three optional identity columns:

* `agent_id` — who wrote it (Codex, Claude, your-internal-bot, …).
  Set via `LOOP_MEMORY_AGENT_ID` env, the SDK default, or per call.
* `user_id` — which user this row belongs to. Useful when one
  loop-memory instance serves multiple users. Defaults to `None`
  (single-user mode).
* `external_id` — a stable per-Agent id (tool name + args hash, a
  UUID minted by the Agent, a content hash, …). Together with
  `(agent_id, user_id)` this forms the upsert key.

A memory with `agent_id IS NULL` and `user_id IS NULL` is **global**
and visible to every caller. Set them when you want isolation.

## Python SDK (in-process)

```python
from loop_memory import MemoryStore
from loop_memory.sdk import MemoryClient

store = MemoryStore("~/.loop_memory/loop_memory.db")
client = MemoryClient.memory(store, agent_id="code-reviewer", user_id="alice")

# Remember a fact. external_id keeps the call idempotent.
client.remember(
    "the orders service uses Postgres + Redis",
    kind="fact",
    importance=0.7,
    tags=["infra", "db"],
    external_id="orders-stack",
)

# Update in place
client.remember(
    "the orders service now uses Postgres + DragonflyDB",
    kind="fact", importance=0.8, external_id="orders-stack",
)

# Recall context. The result is split across memories / wiki / entities
# plus a temporal_intent hint you can pass to the model.
hits = client.recall("orders", limit=5)
for m in hits.memories:
    print(m.external_id, m.text)

# Send feedback
client.feedback(external_id="orders-stack", value="up")
client.feedback(external_id="orders-stack", value="ignore")  # soft-delete

# Forget
client.forget(external_id="orders-stack")
```

## HTTP / JSON (any language)

Bring up the server (`loop-memory serve`) and POST to the v1
surface. Every endpoint is documented in the OpenAPI schema at
`/openapi.json`; the short version is:

| Method | Path                                  | Purpose                              |
| ------ | ------------------------------------- | ------------------------------------ |
| POST   | `/api/v1/memories`                    | Upsert one memory                    |
| POST   | `/api/v1/memories:batch`              | Upsert up to 500 memories at once    |
| GET    | `/api/v1/memories`                    | List with simple filters             |
| GET    | `/api/v1/recall?q=…`                  | Hybrid recall (memories + wiki + entities) |
| POST   | `/api/v1/memories/{id}/feedback`      | 👍/👎 on a memory by id              |
| POST   | `/api/v1/memories/feedback`           | 👍/👎 by `(agent_id, external_id)`    |
| DELETE | `/api/v1/memories`                    | Forget by `(agent_id, external_id)`  |
| DELETE | `/api/v1/memories/{id}`               | Forget by id                         |

Example: idempotent remember with cURL.

```bash
curl -X POST http://127.0.0.1:7767/api/v1/memories \
     -H 'Content-Type: application/json' \
     -d '{
       "text": "team uses Postgres for orders",
       "kind": "fact",
       "importance": 0.7,
       "agent_id": "code-reviewer",
       "user_id": "alice",
       "external_id": "orders-stack"
     }'
```

## MCP (zero-dep stdio)

`loop-memory mcp` is a small JSON-RPC 2.0 server over stdio. It
exposes read tools (`recall`, `list_wiki`, `get_wiki`,
`recent_memories`, `wiki_summary`) and write/management tools. The
v7 graph and cognitive extensions are listed in
[`docs/universal-agent-memory.md`](universal-agent-memory.md):

| Tool       | Args (key ones)                                            | Notes                                  |
| ---------- | ---------------------------------------------------------- | -------------------------------------- |
| `remember` | `text`, `kind?`, `importance?`, `tags?`, `external_id?`    | auto-stamped with `LOOP_MEMORY_AGENT_ID` |
| `forget`   | `id?` or `external_id?` (+ optional `agent_id`, `user_id`) | returns rows deleted                   |
| `feedback` | `value` (`up`/`down`/`ignore`) + address                   | mirrors the HTTP feedback endpoint     |
| `remember_edge` | `src`, `dst`, `kind?`, `weight?`                    | add a semantic graph edge               |
| `subgraph` | `q`, `max_nodes?`, `max_edges?`                            | retrieve graph context                 |
| `cognitive_sleep` | `apply?`, cleanup thresholds                         | suggest or apply cleanup               |
| `audit` | `kind?`, `action?`, `limit?`                                  | read the cognitive audit trail         |

Set the agent identity in the environment so every call from this
process is auto-stamped:

```bash
LOOP_MEMORY_AGENT_ID=code-reviewer LOOP_MEMORY_USER_ID=alice loop-memory mcp
```

## Why the same shape in three places?

Because every Agent has different transport constraints and we don't
want to force a stack on anyone:

* A Python Agent gets the in-process SDK (no serialisation cost, can
  embed into a long-running daemon).
* A Go / Rust / TypeScript / shell Agent gets the HTTP surface
  (zero deps, just JSON over `urllib`/fetch).
* An MCP-aware Agent (Codex CLI, Claude Code, Hermes, …) gets the
  stdio tools with no extra integration.

The three are inter-changeable: anything you can `remember` via the
SDK you can `recall` via HTTP, and vice versa, because the rows
share the same store, the same `external_id` key, and the same
hybrid recall pipeline.

## Migration & compatibility

* The new `agent_id` / `user_id` / `external_id` columns are
  added with a one-shot `ALTER TABLE` in `_init_schema` so existing
  DBs pick them up on the next open. No data backfill is needed.
* Existing rows simply have `NULL` in the new columns; they are
  treated as "global" memories and visible to every Agent.
* The `SCHEMA_VERSION` constant is now `"7"`; v6 identity columns are
  followed by Wiki versioning, cognitive audit, and token metadata tables.
* The existing `/api/memories` / `/api/recall` / `/api/memories/{id}/feedback`
  routes continue to work unchanged.
