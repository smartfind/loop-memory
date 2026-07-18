"""Optional OpenAI adapter.

Only imported when the user has the ``openai`` package installed
(``pip install loop-memory[openai]``). Keeps the core library
zero-dependency.
"""

from __future__ import annotations

from ..llm.base import ChatHistory, LLMClient


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
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=msgs,
            temperature=kwargs.get("temperature", 0.4),
            max_tokens=kwargs.get("max_tokens", 600),
        )
        return resp.choices[0].message.content or ""
