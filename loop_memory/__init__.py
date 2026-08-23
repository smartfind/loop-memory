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

# Single source of truth for the version string: read it from the
# installed distribution metadata so `loop_memory.__version__` always
# matches what `pip show loop-memory` reports. When the package is run
# from a source checkout (no installed metadata) we fall back to
# "0.0.0+source" instead of a hard-coded string — this was the root
# cause of the long-standing stale "0.2.0" since 0.3.0.
try:
    from importlib.metadata import version as _dist_version, PackageNotFoundError
    __version__ = _dist_version("loop-memory")
except PackageNotFoundError:
    __version__ = "0.0.0+source"

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
