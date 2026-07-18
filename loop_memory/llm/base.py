"""LLM client interface.

The Loop Engine never instantiates an LLM directly — it always talks
to one through ``LLMClient``. Bundle ``EchoLLM`` for offline runs and
quick tests, and provide ``ChatHistory`` to standardize the prompt
shape passed to the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatHistory:
    system: str | None = None
    messages: list[Message] = field(default_factory=list)

    def to_prompt(self) -> str:
        parts: list[str] = []
        if self.system:
            parts.append(f"[SYSTEM]\n{self.system}\n")
        for m in self.messages:
            parts.append(f"[{m.role.upper()}]\n{m.content}\n")
        return "\n".join(parts)


class LLMClient(Protocol):
    """A minimal protocol for chat-style LLMs."""

    model: str

    def complete(self, history: ChatHistory, **kwargs) -> str: ...


class EchoLLM:
    """A no-API-key fallback.

    Returns a short acknowledgement plus the *last user message*.
    Useful for unit-testing the loop wiring without burning tokens.
    The engine still runs end-to-end against this client.
    """

    model: str = "echo"

    def __init__(self, prefix: str = "(ok) ") -> None:
        self.prefix = prefix

    def complete(self, history: ChatHistory, **kwargs) -> str:
        for m in reversed(history.messages):
            if m.role == "user":
                # Pull the trailing "USER: ..." line so the echo is short
                # and stable rather than dumping the full reconstructed prompt.
                tail = m.content.rsplit("USER:", 1)[-1].strip()
                tail = tail.split("\n", 1)[0]
                return f"{self.prefix}heard: {tail[:160]}"
        return f"{self.prefix}(empty)"


class SimpleCompletionLLM:
    """Reference implementation that talks to a ``completion`` callable.

    Useful as a template for building real adapters:

        >>> llm = SimpleCompletionLLM(lambda prompt: openai_chat(prompt))
    """

    def __init__(self, completion, model: str = "simple") -> None:
        self._completion = completion
        self.model = model

    def complete(self, history: ChatHistory, **kwargs) -> str:
        return self._completion(history.to_prompt(), **kwargs)
