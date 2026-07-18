# LLM providers

Loop Memory treats the LLM as a pluggable component. All providers are
registered in a single dict at `loop_memory/llm/providers.py → PROVIDERS`. To
add a new one, append a `ProviderSpec` — see **CONTRIBUTING.md** for the
step-by-step.

## Built-in providers

| Provider | Default model | API key | Wire format | Notes |
| --- | --- | --- | --- | --- |
| `MiniMax` | `MiniMax-M2.7` | yes | OpenAI-compatible (`/v1/chat/completions`) | Default. API: <https://platform.minimaxi.com/docs/api-reference/api-overview> |
| `openai` | `gpt-4o-mini` | yes | OpenAI native | Works with any OpenAI-compatible base URL via `base_url` |
| `anthropic` | `claude-3-5-haiku-latest` | yes | Anthropic messages | |
| `ollama` | `qwen2.5:7b` | no | OpenAI-compatible local server | Set `base_url=http://127.0.0.1:11434/v1` |
| `echo` | `rules` | no | rules-based fallback | For tests and offline dev |

`MiniMax` and any OpenAI-compatible endpoint share the same adapter; only the
`base_url` differs. The default `MiniMax` `base_url` is
`https://api.minimaxi.com/v1` (the v2 endpoint; older `.chat` URLs are
deprecated).

## How the provider is chosen

The selection chain at LLM-call time is:

1. The saved `provider` + `model` in **Settings → Models**.
2. If unset, environment variables (`LOOP_MEMORY_API_KEY`,
   `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) are tried in order, and the matching
   provider spec is picked.
3. If nothing is configured, the pipeline falls back to the deterministic
   `echo` rules engine so the UI stays usable offline. A red dot on the
   top-bar **Models** chip indicates this fallback.

## Token limits (v2)

The default behaviour block is tuned for the new "completeness over
compactness" distillation policy:

```jsonc
"behaviour": {
  "batch_size": 50,
  "min_importance": 0.0,
  "max_text_chars": 4000,      // input per LLM call
  "max_output_tokens": 4096,   // output per LLM call
  "temperature": 0.3
}
```

The validator clamps `max_output_tokens` to the range **[64, 8192]** so a
provider that supports longer contexts (e.g. MiniMax-M2.7) can be raised
without code changes.

## API-key storage

Keys are **never** stored in SQLite or echoed back to the browser. They are
written through `loop_memory/security/secrets.py`, which picks the right
backend for the host:

| Platform | Backend | Location |
| --- | --- | --- |
| macOS | Keychain (user-login) | "Loop Memory" service |
| Linux / headless | Local file | `~/.loop_memory/secrets.json` (mode 0600) |

The status endpoint returns only a short `api_key_fingerprint` (e.g.
`ChYM·da7ff5`) so you can confirm a key is set without seeing it.

## Adding a new provider (quick form)

```python
# loop_memory/llm/providers.py
PROVIDERS["my-co"] = ProviderSpec(
    label="MyCo",
    default_model="myco-3-mini",
    default_base_url="https://api.myco.example/v1",
    needs_api_key=True,
    adapter="openai_compat",   # reuse OpenAI adapter if compatible
    notes="OpenAI-compatible chat-completions endpoint.",
)
```

Restart the server. The new provider appears in **Settings → Models**. Click
**Test** to validate the key, then **Save**.

If the wire format is not OpenAI-compatible, drop a custom adapter at
`loop_memory/llm/myco_adapter.py` implementing the `LLMClient` protocol, and
set `adapter="myco"`.

## Troubleshooting

- **401 `login fail: Please carry the API secret key in the 'Authorization'
  field of the request header (1004)`** — the key is missing or wrong; re-paste
  it in **Settings → Models** and click **Test**.
- **`invalid api key (2049)`** — usually a base-URL mismatch: the provider
  expects a different endpoint. Check **Settings → Models → Advanced →
  Base URL**.
- **`404 model_not_found`** — the chosen `model` string is not available on
  the current provider; pick a model from the dropdown or leave it empty to
  use the default.
