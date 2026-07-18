"""Shared helpers for CLI commands.

Kept in a single tiny module so the per-command files can stay
focused on dispatch logic instead of repeating flag parsing and
default paths.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..backends.embedding import HashingEmbedder
from ..engine.loop import LoopEngine
from ..llm.base import EchoLLM

DEFAULT_DB = os.environ.get(
    "LOOP_MEMORY_DB",
    str(Path.home() / ".loop_memory" / "loop_memory.db"),
)


def default_db_path() -> str:
    """Resolve the DB path at call time (not import time).

    Use this instead of the module-level ``DEFAULT_DB`` whenever the
    caller may run after the test suite (or any embedded host) has
    changed ``$LOOP_MEMORY_DB``.
    """
    return os.environ.get(
        "LOOP_MEMORY_DB",
        str(Path.home() / ".loop_memory" / "loop_memory.db"),
    )



def make_engine(db_path: str | None = None) -> LoopEngine:
    """Build a LoopEngine with the default echo LLM + hashing embedder.

    Used by ``chat`` and any other command that drives the engine
    end-to-end without an external LLM.
    """
    return LoopEngine(llm=EchoLLM(), embedder=HashingEmbedder(dim=128))


def parse_int_flag(args: list, name: str, default: int) -> tuple[int, list]:
    """Pop ``--name <int>`` from ``args`` if present.

    Returns the parsed integer (or ``default``) plus the remaining
    args. Non-integer values fall back to ``default`` so we never
    crash the whole command on a typo.
    """
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            try:
                v = int(args[i + 1])
                return v, args[:i] + args[i + 2:]
            except ValueError:
                pass
    return default, list(args)


def die(msg: str, code: int = 2) -> int:
    """Print ``msg`` to stderr and return ``code``."""
    print(msg, file=sys.stderr)
    return code
