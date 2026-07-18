"""Filesystem watcher — auto-ingests **finished** transcripts.

Sister to ``loop-memory hook``. Watches a directory for transcripts
written by Codex CLI / Claude Code / Hermes and ingests **only when a
transcript is "done"**:

  * it has not been modified for ``idle_seconds`` (default 60s), and
  * the size is stable across the same idle window.

This means a 30-minute chat that just ended is picked up ~60 seconds
after the user (or the CLI's auto-save) finished writing. Active
typing that mutates the file every few seconds is **not** picked up.

Already-ingested files are tracked in a small JSON ledger so re-runs
don't double-write.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..ingest.loader import BaseLoader
from ..ingest.pipeline import MemoryPipeline

log = logging.getLogger("loop_memory.watcher")


def _ledger_path(watch_dir: Path) -> Path:
    return Path(watch_dir).expanduser() / ".loop_memory_seen.json"


def _load_ledger(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ledger(path: Path, ledger: dict) -> None:
    try:
        path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.exception("failed to persist ingest ledger at %s", path)


def run_watcher(
    loader: BaseLoader,
    watch_dir: Path,
    pipeline: MemoryPipeline,
    poll_seconds: float = 2.0,
    idle_seconds: float = 60.0,
    ledger: Optional[dict] = None,
    on_ingest: Optional[callable] = None,
) -> None:
    """Watch a directory and ingest each transcript once it has been idle
    for ``idle_seconds``.

    ``ledger`` is a dict ``path → {mtime, size, ingested_at}`` used for
    idempotency. Pass in to share state across processes, leave None
    to use the default JSON file under ``watch_dir``.

    ``on_ingest`` is an optional callable invoked with no arguments
    after a successful ingest. The serve layer hooks this to a
    consolidator scheduler so ``realtime`` mode can fire.
    """
    watch_dir = Path(watch_dir).expanduser()
    watch_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = _ledger_path(watch_dir)
    if ledger is None:
        ledger = _load_ledger(ledger_path)

    log.info(
        "watching %s for %s transcripts (idle>=%.0fs, poll=%.1fs)",
        watch_dir, loader.source, idle_seconds, poll_seconds,
    )

    def persist():
        _save_ledger(ledger_path, ledger)

    try:
        while True:
            try:
                files = list(loader.discover(watch_dir))
            except FileNotFoundError:
                files = []

            now = time.time()
            for path in files:
                key = str(path)
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue

                if path.name == ".loop_memory_seen.json":
                    continue

                sig = (st.st_mtime, st.st_size)
                prev = ledger.get(key)

                # Already-ingested with same signature → skip.
                if prev and prev.get("sig") == list(sig):
                    continue
                # Already-ingested but file changed → treat as a new
                # session appended to the same file. Reset idle timer.
                if prev and prev.get("ingested_at"):
                    ledger[key] = {
                        "sig": list(sig),
                        "first_seen": now,
                        "last_mtime": st.st_mtime,
                        "size": st.st_size,
                        "ingested_at": None,
                    }
                    continue

                # First observation: stamp it.
                if not prev:
                    ledger[key] = {
                        "sig": list(sig),
                        "first_seen": now,
                        "last_mtime": st.st_mtime,
                        "size": st.st_size,
                        "ingested_at": None,
                    }
                    persist()
                    continue

                # Subsequent observation: only proceed if the file has
                # been idle for ``idle_seconds``.
                if (now - st.st_mtime) < idle_seconds:
                    continue

                # Stable and idle → ingest once.
                try:
                    session = loader.load_one(path)
                except Exception:
                    log.exception("loader failed on %s", path)
                    session = None

                if session is not None:
                    try:
                        result = pipeline.run(session)
                        log.info(
                            "ingested %s as %s (%d summary items)",
                            path.name, session.source, len(result.summary_items),
                        )
                        if on_ingest is not None:
                            try:
                                on_ingest()
                            except Exception:
                                log.exception("on_ingest callback failed")
                    except Exception:
                        log.exception("pipeline failed on %s", path)

                ledger[key] = {
                    "sig": list(sig),
                    "first_seen": prev.get("first_seen", now),
                    "last_mtime": st.st_mtime,
                    "size": st.st_size,
                    "ingested_at": now,
                }
                persist()

            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        log.info("watcher exiting")
        persist()
