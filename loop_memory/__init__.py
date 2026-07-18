"""Loop Engineering Memory System for Large Language Models.

A zero-dependency framework that gives any LLM a persistent
``Retrieve → Generate → Reflect → Store`` loop plus a tiered memory
(short / long / episodic / procedural) and a SQLite-backed local store
for cross-session recall.

Optional extras:

    pip install loop-memory[openai]    # OpenAIClient
    pip install loop-memory[chroma]    # ChromaVectorStore
    pip install loop-memory[sentence]  # SentenceTransformerEmbedder
    pip install loop-memory[serve]     # FastAPI + uvicorn for the local UI
    pip install loop-memory[all]       # everything
"""

from .backends.embedding import BaseEmbedder, HashingEmbedder, IdentityEmbedder
from .backends.vector_store import (
    ChromaVectorStore,
    InMemoryVectorStore,
    VectorStore,
)
from .engine.loop import LoopEngine, LoopResult
from .llm.base import ChatHistory, EchoLLM, LLMClient, Message
from .memory.types import (
    EpisodicMemory,
    LongTermMemory,
    MemoryItem,
    ProceduralMemory,
    ShortTermMemory,
)
from .storage.sqlite_store import MemoryStore, StoredMemory, StoredSession

__version__ = "0.2.0"

__all__ = [
    # engine
    "LoopEngine",
    "LoopResult",
    # memory
    "MemoryItem",
    "ShortTermMemory",
    "LongTermMemory",
    "EpisodicMemory",
    "ProceduralMemory",
    # llm
    "LLMClient",
    "EchoLLM",
    "ChatHistory",
    "Message",
    # backends
    "BaseEmbedder",
    "HashingEmbedder",
    "IdentityEmbedder",
    "VectorStore",
    "InMemoryVectorStore",
    "ChromaVectorStore",
    # storage
    "MemoryStore",
    "StoredMemory",
    "StoredSession",
]
