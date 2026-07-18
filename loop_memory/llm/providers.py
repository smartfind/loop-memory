"""Pluggable LLM providers for memory consolidation.

The pipeline talks to any LLM through this small abstraction, so
the same loop_memory / consolidate / score / summarize machinery
works against:

* OpenAI-compatible HTTP APIs (OpenAI, Azure, OpenRouter, vLLM,
  LM Studio, llama.cpp server) - ``OpenAICompatProvider``
* Anthropic Messages API - ``AnthropicProvider``
* Ollama's ``/api/chat`` - ``OllamaProvider``
* A no-key offline fallback - ``RuleBasedProvider`` (the default
  when the user has not configured anything)

Adding a new provider is one class with a ``complete()`` method
that turns a ``ChatHistory`` into a string.
"""

from __future__ import annotations

import json
import logging
import re
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .base import ChatHistory, LLMClient

log = logging.getLogger(__name__)


@dataclass
class ProviderSpec:
    """Static metadata about a provider for the UI dropdown."""
    id: str
    label: str
    default_model: str
    needs_api_key: bool
    needs_base_url: bool
    default_base_url: str | None = None
    description: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "MiniMax": ProviderSpec(
        id="MiniMax",
        label="MiniMax",
        default_model="MiniMax-M2.7",
        needs_api_key=True,
        needs_base_url=True,
        default_base_url="https://api.minimaxi.com/v1",
        description="MiniMax (https://platform.minimaxi.com) — recommended default",
    ),
    "openai": ProviderSpec(
        id="openai",
        label="OpenAI-compatible",
        default_model="gpt-4o-mini",
        needs_api_key=True,
        needs_base_url=True,
        default_base_url="https://api.openai.com/v1",
        description="OpenAI / Azure OpenAI / OpenRouter / vLLM / LM Studio / llama.cpp server",
    ),
    "anthropic": ProviderSpec(
        id="anthropic",
        label="Anthropic",
        default_model="claude-3-5-haiku-latest",
        needs_api_key=True,
        needs_base_url=False,
        default_base_url="https://api.anthropic.com",
        description="Anthropic Claude (Messages API)",
    ),
    "ollama": ProviderSpec(
        id="ollama",
        label="Ollama (local)",
        default_model="qwen2.5:7b",
        needs_api_key=False,
        needs_base_url=True,
        default_base_url="http://127.0.0.1:11434",
        description="Local Ollama daemon - no API key, fully offline",
    ),
    "echo": ProviderSpec(
        id="echo",
        label="Rule-based (offline, no LLM)",
        default_model="rules",
        needs_api_key=False,
        needs_base_url=False,
        description="Deterministic rules - no network, no API cost, decent baseline",
    ),
}


class LLMHttpError(RuntimeError):
    """HTTP error from an LLM provider with structured fields attached.

    Attributes
    ----------
    status : int
        HTTP status code (e.g. 401, 429, 500).
    url : str
        The endpoint that was called.
    raw : str
        Raw response body (truncated to 500 chars).
    provider_code : str | None
        Provider-specific error code when the body is JSON, e.g. MiniMax's
        ``2049`` ("invalid api key") or ``1004`` ("Please carry the API key").
    provider_message : str | None
        Human-readable provider error message when present.
    body_json : dict | None
        The full parsed JSON body when the response is JSON, else None.
    """

    def __init__(self, status: int, url: str, raw: str) -> None:
        self.status = int(status)
        self.url = url
        self.raw = (raw or "")[:500]
        self.body_json: dict | None = None
        self.provider_code: str | None = None
        self.provider_message: str | None = None
        try:
            parsed = json.loads(self.raw)
            if isinstance(parsed, dict):
                self.body_json = parsed
                err = parsed.get("error") or {}
                # Extract a numeric MiniMax/OpenAI code when present in the
                # message itself ("invalid api key (2049)"). This is the
                # code the user will search the docs for.
                msg = None
                if isinstance(err, dict):
                    self.provider_code = (
                        str(err.get("code"))
                        or str(err.get("type"))
                        or None
                    )
                    msg = err.get("message")
                elif isinstance(err, str):
                    msg = err
                if msg:
                    self.provider_message = msg
                    m = re.search(r"\((\d{3,5})\)", msg)
                    if m and not (self.provider_code and self.provider_code.isdigit()):
                        self.provider_code = m.group(1)
        except Exception:
            pass
        msg = f"LLM HTTP {self.status}"
        if self.provider_message:
            msg += f": {self.provider_message[:200]}"
        if self.provider_code:
            msg += f" (code={self.provider_code})"
        super().__init__(msg)


def _http_post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")
        log.error("LLM HTTP %s @ %s: %s", e.code, url, err[:400])
        raise LLMHttpError(e.code, url, err)


class OpenAICompatProvider(LLMClient):
    """OpenAI-compatible chat completions client.

    Works against any server that exposes POST /chat/completions
    with model+messages and returns choices[0].message.content.
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 20.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("LOOP_MEMORY_API_KEY")
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.timeout = timeout

    def complete(self, history: ChatHistory, **kwargs) -> str:
        msgs: list[dict[str, str]] = []
        if history.system:
            msgs.append({"role": "system", "content": history.system})
        for m in history.messages:
            msgs.append({"role": m.role, "content": m.content})
        body = {
            "model": self.model,
            "messages": msgs,
            "temperature": float(kwargs.get("temperature", 0.3)),
            "max_tokens": int(kwargs.get("max_tokens", 800)),
        }
        url = self.base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = _http_post_json(url, body, headers, self.timeout)
        return (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") or ""


class AnthropicProvider(LLMClient):
    """Anthropic Messages API (claude-3.x)."""

    def __init__(
        self,
        model: str = "claude-3-5-haiku-latest",
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 20.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.timeout = timeout

    def complete(self, history: ChatHistory, **kwargs) -> str:
        sys_prompt = history.system or ""
        msgs: list[dict[str, str]] = []
        for m in history.messages:
            if m.role == "system":
                sys_prompt += "\n" + m.content
                continue
            msgs.append({"role": m.role, "content": m.content})
        body = {
            "model": self.model,
            "system": sys_prompt or "You are a helpful assistant.",
            "messages": msgs,
            "max_tokens": int(kwargs.get("max_tokens", 800)),
            "temperature": float(kwargs.get("temperature", 0.3)),
        }
        url = self.base_url + "/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key or "",
            "anthropic-version": "2023-06-01",
        }
        data = _http_post_json(url, body, headers, self.timeout)
        content = data.get("content") or []
        parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(parts).strip()


class OllamaProvider(LLMClient):
    """Ollama /api/chat - local LLMs with no API key."""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")
        self.timeout = timeout

    def complete(self, history: ChatHistory, **kwargs) -> str:
        msgs: list[dict[str, str]] = []
        if history.system:
            msgs.append({"role": "system", "content": history.system})
        for m in history.messages:
            msgs.append({"role": m.role, "content": m.content})
        body = {
            "model": self.model,
            "messages": msgs,
            "stream": False,
            "options": {
                "temperature": float(kwargs.get("temperature", 0.3)),
                "num_predict": int(kwargs.get("max_tokens", 800)),
            },
        }
        url = self.base_url + "/api/chat"
        data = _http_post_json(url, body, {"Content-Type": "application/json"}, self.timeout)
        return (data.get("message") or {}).get("content") or ""


class RuleBasedProvider(LLMClient):
    """A deterministic, zero-network fallback.

    Used when the user has not configured an LLM. The consolidator
    already runs deterministic rules for noise filtering, so this
    provider mostly returns a short placeholder so the LLM-call
    site in the pipeline still has something to parse.
    """

    model = "rules"

    def __init__(self, min_chars: int = 6) -> None:
        self.min_chars = min_chars

    def complete(self, history: ChatHistory, **kwargs) -> str:
        last_user = ""
        for m in reversed(history.messages):
            if m.role == "user":
                last_user = m.content
                break
        return f"(rules) {last_user[:120]}"


def resolve_api_key(config: dict[str, Any]) -> str | None:
    """Look up the API key for a provider through the secret backend.

    The settings JSON never contains the key itself — only an
    ``api_key_account`` field like ``"llm/openai/api_key"`` and a
    boolean ``api_key_set``. This function returns the actual
    secret material, reading from the configured local backend when necessary.
    """
    if not isinstance(config, dict):
        config = {}
    ptype = (config.get("provider") or "echo").lower()
    explicit = config.get("api_key")
    if explicit:
        return explicit
    account = config.get("api_key_account")
    if not account:
        # Default account name for the provider, used as a sensible
        # fallback so the user doesn't have to manage multiple.
        account = f"llm/{ptype}/api_key"
    try:
        from ..security import get_secret
        return get_secret(account)
    except Exception:
        return None


def build_provider(config: dict[str, Any]) -> LLMClient:
    """Build a provider from a settings dict (see UI schema).

    Safety fallback: if the chosen provider requires an API key but
    none has been configured, drop to the rule-based provider instead
    of attempting a 60-second network timeout per batch. The user is
    warned via the returned provider name + a one-line log so the UI
    can also surface a "no key configured" badge.
    """
    if not isinstance(config, dict):
        config = {}
    ptype = str(config.get("provider") or "echo").lower()
    if ptype not in PROVIDERS:
        match = next((k for k in PROVIDERS if k.lower() == ptype), None)
        if match is not None:
            ptype = match
    spec = PROVIDERS.get(ptype, PROVIDERS["echo"])
    model = config.get("model") or spec.default_model
    api_key = resolve_api_key(config)
    base_url = config.get("base_url") or spec.default_base_url
    if spec.needs_api_key and not api_key:
        log.warning(
            "provider %s requires an API key but none is configured; "
            "falling back to rule-based provider (no network calls).",
            ptype,
        )
        return RuleBasedProvider()
    if ptype == "MiniMax":
        return OpenAICompatProvider(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://api.minimaxi.com/v1",
        )
    if ptype in ("openai", "openai_compat", "openai-compat"):
        return OpenAICompatProvider(model=model, api_key=api_key, base_url=base_url or "https://api.openai.com/v1")
    if ptype == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key, base_url=base_url or "https://api.anthropic.com")
    if ptype == "ollama":
        return OllamaProvider(model=model, base_url=base_url or "http://127.0.0.1:11434")
    if ptype in ("rules", "echo"):
        return RuleBasedProvider()
    log.warning("unknown LLM provider %r, falling back to rules", ptype)
    return RuleBasedProvider()


def default_config() -> dict[str, Any]:
    """Settings shape the UI uses as a starting point.

    The ``api_key`` field is never persisted to the SQLite store.
    The actual secret lives in the OS keychain under the account
    name ``api_key_account``; the settings blob only carries a
    boolean ``api_key_set`` hint so the UI can show a "key
    configured" badge.
    """
    return {
        "provider": "echo",
        "model": "rules",
        "api_key_set": False,
        "api_key_account": "llm/echo/api_key",
        "base_url": "",
        "schedule": {
            "enabled": False,
            "mode": "off",                # off | realtime | hourly | daily | weekly | interval
            "interval_minutes": 60,       # for "every N minutes"
            "hour": 3,                    # for daily
            "minute": 0,
            "weekday": 0,                 # 0=Mon, 6=Sun, for weekly
            "after_ingest_idle_sec": 30,  # realtime: wait this long after last ingest
        },
        "behaviour": {
            "batch_size": 50,
            "min_importance": 0.0,
            "max_text_chars": 1200,
            "max_output_tokens": 800,
            "temperature": 0.3,
            "enable_score": True,         # re-score by LLM
            "enable_filter": True,        # drop noise
            "enable_summarize": True,     # condense near-dupes
            "dry_run": False,             # if true, do not mutate the store
        },
    }


def validate_config(cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize + validate. Returns (cleaned, warnings)."""
    warnings: list[str] = []
    if not isinstance(cfg, dict):
        cfg = {}
    ptype = str(cfg.get("provider") or "echo").lower()
    if ptype not in PROVIDERS:
        # Try case-insensitive fallback so providers can be entered as
        # "MiniMax", "minimax", "MINIMAX" and still resolve.
        match = next((k for k in PROVIDERS if k.lower() == ptype), None)
        if match is not None:
            ptype = match
        else:
            warnings.append(f"unknown provider {ptype!r}; using 'echo'")
            ptype = "echo"
    spec = PROVIDERS[ptype]
    out = dict(cfg)
    out["provider"] = ptype
    # The api_key field is *never* stored in the settings blob; strip
    # it on the way through. Callers should go through the keychain
    # or send a one-off api_key to the test endpoint.
    out.pop("api_key", None)
    if "api_key_set" not in out:
        out["api_key_set"] = False
    if not out.get("model"):
        out["model"] = spec.default_model
    # api_key is no longer in this dict. The keychain is checked at
    # build_provider() time. The settings table just carries
    # api_key_set / api_key_account hints.
    if not out.get("api_key_account"):
        out["api_key_account"] = f"llm/{ptype}/api_key"
    if spec.needs_api_key and not bool(out.get("api_key_set")):
        env = (os.environ.get("LOOP_MEMORY_API_KEY")
               or os.environ.get("OPENAI_API_KEY")
               or os.environ.get("ANTHROPIC_API_KEY"))
        if env:
            out["api_key_set"] = True
            # We do NOT store the env key into the keychain — that
            # would surprise the user. We just note that an env-based
            # fallback is available.
            warnings.append("API key not set in keychain; falling back to env var.")
    if spec.needs_base_url and not (out.get("base_url") or "").strip():
        out["base_url"] = spec.default_base_url
    if not isinstance(out.get("schedule"), dict):
        out["schedule"] = default_config()["schedule"]
    if not isinstance(out.get("behaviour"), dict):
        out["behaviour"] = default_config()["behaviour"]
    beh = out["behaviour"]
    try:
        beh["batch_size"] = max(1, min(int(beh.get("batch_size") or 50), 500))
    except Exception:
        beh["batch_size"] = 50
    try:
        beh["max_output_tokens"] = max(64, min(int(beh.get("max_output_tokens") or 800), 4096))
    except Exception:
        beh["max_output_tokens"] = 800
    try:
        beh["temperature"] = max(0.0, min(float(beh.get("temperature") or 0.3), 2.0))
    except Exception:
        beh["temperature"] = 0.3
    try:
        beh["min_importance"] = max(0.0, min(float(beh.get("min_importance") or 0.0), 1.0))
    except Exception:
        beh["min_importance"] = 0.0
    out["behaviour"] = beh
    return out, warnings
