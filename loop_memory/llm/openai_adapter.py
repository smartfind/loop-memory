"""Optional OpenAI adapter.

Only imported when the user has the ``openai`` package installed
(``pip install loop-memory[openai]``). Keeps the core library
zero-dependency.
"""

from __future__ import annotations

import os

from ..llm.base import ChatHistory, LLMClient


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class OpenAIClient(LLMClient):
    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None) -> None:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise RuntimeError("openai is not installed; pip install loop-memory[openai]") from e
        self.model = model
        self._client = OpenAI(api_key=api_key)  # type: ignore[arg-type]

    def complete(self, history: ChatHistory, **kwargs) -> str:
        msgs = [{"role": "system", "content": history.system}] if history.system else []
        msgs += [{"role": m.role, "content": m.content} for m in history.messages]
        temperature = _env_float(
            "LLM_TEMPERATURE",
            float(kwargs.get("temperature", 0.4)),
        )
        create_kwargs = dict(
            model=self.model,
            messages=msgs,
            temperature=float(temperature if temperature is not None else 0.4),
            max_tokens=int(kwargs.get("max_tokens", 600)),
        )
        seed = _env_optional_int("LLM_SEED")
        if seed is None:
            seed_val = kwargs.get("seed")
            seed = int(seed_val) if seed_val is not None else None
        if seed is not None:
            create_kwargs["seed"] = int(seed)
        resp = self._client.chat.completions.create(**create_kwargs)
        return resp.choices[0].message.content or ""
