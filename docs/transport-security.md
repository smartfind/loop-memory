# Transport security for the remote memory API

> Added in **0.4.21** (audit 2026-11-08). Applies to every client
> that talks to a `loop-memory serve` instance over the network.

## The threat

Loop Memory stores what your agents learned: user preferences,
project decisions, API endpoints, personal details. The store
redacts obvious secrets before they are written
(`loop_memory.privacy.redact`), and the server requires a bearer
token once one is configured. Neither of those protects the **wire**.

Before 0.4.21 the HTTP SDK backend had no transport guard at all:

```python
# Before: silently sends every memory in cleartext.
client = MemoryClient.http("http://memory.example.com:7767")
client.remember("the user's deploy key is named prod-lion")
```

and it could not send a bearer token either, so a remote client could
not talk to a protected server at all — the only way to make it work
was to turn auth off, which is precisely the configuration where the
memory text and the token both cross the network in cleartext.

## The rules

The guard lives in `loop_memory.security.transport` and is about the
**destination**, not the scheme.

| Base URL | Verdict | Why |
| --- | --- | --- |
| `https://memory.example.com` | **allowed** | encrypted end to end |
| `http://127.0.0.1:7767` | **allowed** | plaintext, but the traffic never leaves the host — this is the default deployment |
| `http://[::1]:7767` | **allowed** | same, IPv6 loopback |
| `http://memory.example.com` | **refused** | plaintext to a remote host: the memory text and the bearer token cross the network in cleartext |
| `http://0.0.0.0:7767` | **refused** | `0.0.0.0` is a *wildcard bind* address, not a loopback address |
| `https://user:pw@memory.example.com` | **refused** | credentials in a URL leak into logs and error messages |
| `ftp://memory.example.com` | **refused** | unsupported scheme |

The refusal happens at **construction time**, not in the middle of a
write, so a misconfiguration surfaces before any data moves:

```python
>>> MemoryClient.http("http://memory.example.com")
InsecureTransportError: refusing to send memory data to 'memory.example.com'
over plaintext http:// — the memory text and the bearer token would cross
the network in cleartext.
  Fix: serve the remote instance over https://, or point the client at a
  loopback address.
  If the plaintext genuinely never leaves this machine (SSH tunnel,
  WireGuard, a private LAN), opt in explicitly: allow_insecure=True
  (or LOOP_MEMORY_ALLOW_INSECURE=1).
```

### The opt-in is never silent

A private tunnel, a WireGuard interface, or a VLAN that never leaves
the building are legitimate deployments. `allow_insecure=True`
permits them — and warns, every time:

```python
>>> MemoryClient.http("http://memory.internal:7767", allow_insecure=True)
UserWarning: loop-memory is send memory data to the remote host
'memory.internal' over plaintext http:// because allow_insecure was set
— the payload is not encrypted in transit.
```

The same `allow_insecure` parameter exists on `http_get_json`,
`http_post_json` and `http_delete_json`. A deployment can express the
same decision without touching code:

```bash
export LOOP_MEMORY_ALLOW_INSECURE=1   # 1 / true / yes / on
```

The env var is the deployment-level spelling of the same opt-in — it
permits the request and it **still warns**.

## Authenticating against a protected server

`loop-memory serve` requires `Authorization: Bearer <token>` once a
token exists. The SDK can send it:

```python
client = MemoryClient.http(
    "https://memory.example.com",
    token="lm-…",           # from `curl -X POST .../api/admin/auth/token`
)
client.remember("deploys go out on Tuesdays")
```

The token is never interpolated into a URL and never appears in an
error message — error bodies are truncated server text, and the
client's own messages name the method and the path only.

## Redirects are hardened too

`urllib` follows redirects by default, and it **replays the
`Authorization` header** onto the redirect request. Two hops are
therefore refused:

* **No scheme downgrade.** `https://` → `http://` raises
  `InsecureTransportError`. Otherwise the TLS guarantee would be
  dropped one hop away from the server.
* **No credential-bearing cross-origin redirect.** A 302 to another
  host while a bearer token is attached raises
  `InsecureTransportError` instead of handing the token over.

A redirect that stays on the same origin is followed as before.

## The server side: `loop-memory doctor`

The client guard protects what a client *sends*. The server half is a
new doctor check, `plaintext_exposure`:

```
$ loop-memory doctor
[warn] plaintext_exposure — the API is bound to 0.0.0.0:7767 — it is
       reachable from the network over plaintext http://, so every memory
       payload crosses the wire in cleartext; the bearer token that
       protects those payloads is exposed the same way.
```

`loop-memory serve` records its bind address in the store when it
starts, so the check knows what it is looking at. A loopback bind
reports **ok**; a store that was never served through the CLI reports
**info**, not a warning — the doctor does not cry wolf on a store that
was never exposed in the first place.

To fix the warning, either bind `127.0.0.1` and put a TLS terminator
(nginx, Caddy, a tunnel) in front of it, so clients can use
`https://`, or keep the plaintext only if it provably never leaves the
machine.

## What this does **not** change

* **The MCP server** (`loop-memory mcp`) opens the SQLite database
  directly and never speaks HTTP — it is unaffected.
* **The in-process SDK** (`MemoryClient.memory(store)`) never touches
  the network.
* **The default deployment** — `http://127.0.0.1:7767` — is
  unchanged, and no existing loopback setup needs to change
  anything.
* **Server-side TLS.** Loop Memory does not terminate TLS itself;
  put a terminator in front of it and use `https://` from the client.

## See also

* [`docs/api.md`](./api.md) — the HTTP surface
* [`docs/universal-agent-memory.md`](./universal-agent-memory.md) — the SDK contract
* [`SECURITY.md`](../SECURITY.md) — the reporting policy
* `docs/research/2026-11-08.md` — the ecosystem note this release is
  based on, with the upstream sources it borrows from
