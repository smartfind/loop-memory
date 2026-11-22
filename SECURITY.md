# Security Policy

## Supported versions

| Version | Supported        |
| ------- | ---------------- |
| 0.1.x   | :white_check_mark: |

## What Loop Memory already defends

Loop Memory is local-first, and the layers below are in place before
you report anything:

| Layer | Where | What it does |
| --- | --- | --- |
| Content redaction | `loop_memory/privacy/` | API keys, tokens, private keys, JWTs and high-entropy blobs are redacted **before** a row is written; `<private>…</private>` spans are stripped |
| Secret storage | `loop_memory/security/secrets.py` | provider API keys live in `~/.loop_memory/secrets.json` (mode `0600`), never in the SQLite `settings` table |
| Server auth | `loop-memory serve` | `Authorization: Bearer <token>` on every non-public route, compared with `compare_digest`; the token is stored hashed |
| CSRF / CSP | `loop-memory serve` | Origin-bound CSRF check on every state-changing request, CSP `deny-all` on the UI, markdown sanitisation before render |
| Path safety | `loop_memory/storage/` | the export / snapshot endpoints refuse paths that point at the live store, a system directory, or a sensitive dotfile |
| Audit | `loop_memory/storage/sqlite_store.py` | 12 SQLite triggers make the audit log impossible to skip, even for a raw `sqlite3` writer |
| **Transport** | **`loop_memory/security/transport.py`** | **since 0.4.21: the SDK refuses to send memory data — or the bearer token — over plaintext `http://` to a non-loopback host, refuses URLs with embedded credentials, and refuses redirects that downgrade the scheme or would replay the `Authorization` header** |
| Server exposure | `loop-memory doctor` | `plaintext_exposure` warns when the API is bound to a non-loopback interface and therefore reachable from the network over cleartext |
| **Namespace isolation** | **`loop_memory/storage/sqlite_store.py`** | **since 0.4.22: the agent / user namespace predicate is applied inside the SQL of every retrieval channel, so a scoped caller cannot read another namespace's rows and a foreign namespace cannot consume the caller's result budget** |
| **Namespace isolation on writes** | **`loop_memory/storage/sqlite_store.py`** | **since 0.4.23: a write may only touch rows of its own namespace. The `external_id` re-route, the content-hash dedup and the subject-key supersession all match `(agent_id, user_id)` exactly (null-safe `IS`), so an agent-scoped write can no longer supersede a global row of the same slot, nor collapse onto a global row with the same text and overwrite it. The global namespace `(NULL, NULL)` is a namespace like any other** |

The transport layer is documented in
[`docs/transport-security.md`](docs/transport-security.md); the
`allow_insecure=True` escape hatch (for a private tunnel or a VPN'd
LAN) is explicit and warns on every use.

The namespace layer is documented in
[`docs/namespace-isolation.md`](docs/namespace-isolation.md). A memory
written without an `agent_id` / `user_id` is **global** and visible to
every caller by design — the ingest pipeline, the consolidator and the
dream job all write global rows. A memory that *names* a namespace is
visible only inside it.

Since 0.4.23 the same rule governs **writes**: a write may only touch
rows of its own namespace, so an agent can no longer overwrite shared
global knowledge by writing a row that happens to carry the same
text, nor take over a global logical slot by pushing a new value into
it. Before that release the namespace predicate on the write path had
a second branch that also matched the global rows for every caller
that had an agent but no user.

## Reporting a vulnerability

Please **do not** file a public issue for security problems. Open a
private GitHub Security Advisory instead, or email the maintainer
listed on the repository's main page.

We aim to acknowledge new reports within 72 hours and to ship a fix
or mitigation within 14 days for issues rated High or Critical.
