# Settings drawer

The settings drawer (gear icon in the topbar → `?drawer=settings`) is the single
control surface for the LLM connection, consolidation job, privacy / redaction,
storage budget, and access-token toggle. This page is the authoritative guide
to the choices the drawer surfaces.

## Layout

- Sections are rendered top-down in the order:
  1. **Connection** — LLM provider, model, base URL, API key, schedule,
     consolidation behaviour. Hidden when `?drawer=llm-config` is the only
     surface opened from a "Model not set" tip.
  2. **Storage & compaction** — capacity ceilings and automatic compaction
     cadence. The two "last compact" / "current" cards share the same row so
     the status pill aligns with the number input it pairs with.
  3. **Redaction** — secrets auto-stripped from stored memories and exported
     Markdown. The two switches and the private-span switch share the same
     card pattern; their descriptions sit on a second line aligned with the
     card's left edge.
  4. **Security** — bearer token toggle.
  5. **Manual actions** — `Run now` and `Preview` buttons.
  6. **Recent runs** — last 5 consolidation runs, with next-run and
     `lastCompact` text.

## Connection: API key and provider

- `Save` stores the LLM provider + model + optional base URL and the API
  key. The key is sent in a `PUT` body; the backend stores it in the
  per-machine keychain and returns `api_key_set=true` on subsequent
  reads. The form never displays the saved key.
- `Test` performs a `POST /api/admin/llm/test` round-trip. The result
  appears inline (✅ / ✗) without leaving the drawer.
- `Clear key` opens an inline confirm modal (replacing the browser
  `confirm()` dialog so it works under CSP). The backend treats the
  `__clear__` sentinel as a delete.

## Storage & compaction

- `Capacity` (`max_bytes_mb`, `max_memories`) clamps long-term memory to a
  ceiling. The hint under each input shows the current usage and the
  default — e.g. `Currently 95 MB / default 256 MB`.
- `Compression` toggle enables periodic compaction. When on, two side-by-side
  cards appear: the interval input and a status card showing the last
  compaction time. The two cards share the same height and align at the
  top, so the eye reads the input → result pair as a single row.
- `Save` commits the values; `Compact now` triggers an out-of-band run and
  shows a live hint while the worker is in flight.

## Redaction

- `Enable redaction` is the master switch. Off means secrets land in
  storage unredacted — strongly discouraged.
- `Process <private>...</private> tags` is the per-document opt-in.
  When on, the body between `<private>` markers is replaced with
  `[PRIVATE:redacted]` before storage and before Markdown export.
- `Rule kinds` is a read-only summary of the regex catalog driving the
  auto-strip. Expand to see them.

## Security: access token

The token section exposes a single bearer-token toggle. The behaviour is:

| State | API requests | Notes |
| --- | --- | --- |
| **Disabled** (default) | Anonymous. Any request to `/api/*` is accepted without a credential. Convenient on a single-user machine; **do not use on shared networks**. |
| **Enabled** | The server issues a token via `POST /api/admin/auth/token`. The dashboard stores it in `localStorage` and includes it as `Authorization: Bearer …` on every request. Off-tab callers (curl, scripts, other agents) must opt in by adding the header manually or they will receive `401`. |

Disabling revokes the token on the server side; the next `POST` is
rejected until you re-enable. Re-enabling generates a fresh token — the
old one is no longer valid. Tokens are stored as a SHA-256 hash on the
server; the raw value is only ever returned at create time.

### When to enable

- Multiple agents share the same machine or LAN and you want to keep
  `127.0.0.1:7767` private to your user session.
- The dashboard is exposed via SSH port-forward and you want the listener
  to refuse anonymous API calls.
- You want the audit log to record which agent made each call (the
  bearer token is the only identity the server has).

Leave it **disabled** for a single-user laptop running Loop Memory
locally — the trade-off is one fewer auth step in your day.

## Manual actions

- `Run now` triggers the consolidation job synchronously and shows the
  result count in a toast. Use it when you've just made a change to
  redaction rules or capacity limits and want immediate feedback.
- `Preview` runs the same job in `dry_run` mode and lists which
  memories would be kept, dropped, and merged — without writing
  anything. The button only works when the consolidation schedule
  includes the `behaviour.dry_run` flag; otherwise it returns a
  no-op toast.

## Recent runs

Lists the most recent five runs (`kept`, `dropped`, `rescored`, `merged`,
elapsed time, status). The footer shows the next scheduled run.
