"""Filesystem watcher — auto-ingests **finished** transcripts.

Sister to ``loop-memory hook``. Watches a directory for transcripts
written by Codex CLI / Claude Code / Hermes and ingests **only when a
transcript is "done"**:

  * its **byte size** has not grown for ``idle_seconds`` (default 60s).

We intentionally do NOT key on mtime alone: Codex desktop (and similar
agents) refresh the file mtime on background metadata flushes even
when no new content is being written. Treating those as "still being
written" would prevent an ingest from ever firing for long, active
sessions. Size-stable-for-N-seconds is the correct signal.

This means a 30-minute chat that just ended is picked up ~60 seconds
after the user (or the CLI's auto-save) finished writing. Active
typing that grows the file size every few seconds is **not** picked up,
but pure metadata flushes on an idle file **are**.

Already-ingested files are tracked in a small JSON ledger so re-runs
don't double-write.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from ..ingest.loader import BaseLoader
from ..ingest.pipeline import MemoryPipeline

log = logging.getLogger("loop_memory.watcher")
# Make ``watching ...`` / ``watcher settings reloaded: ...`` lines
# visible in the hook process log without requiring every caller to
# configure logging first. ``basicConfig`` is a no-op once the root
# logger already has a handler, so importing this module from the
# serve app or from tests doesn't disturb their log formatting.
if not logging.getLogger().handlers:
    _level_name = os.environ.get("LOOP_MEMORY_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, _level_name, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )


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


# Default knobs when the user has no persisted ingest settings yet.
# We default to 5 minutes (300s) of size-stable idle before ingesting:
# shorter intervals fragment long conversations into multiple partial
# memories, longer intervals delay recall. The user can dial this up
# or down from Settings → 采集频率. ``poll_seconds`` defaults to 5
# (vs the previous 2.0) to cut down on stat() churn on the watched
# directory — stat is cheap on SSD but very cheap on the order of
# seconds; not the order of milliseconds. The user can always tune
# both via the Settings drawer.
DEFAULT_IDLE_SECONDS = 300.0
DEFAULT_POLL_SECONDS = 5.0


def _read_ingest_settings(store) -> tuple[float, float]:
    """Pull ``ingest.idle_seconds`` / ``ingest.poll_seconds`` from the
    settings store. Missing keys fall back to module defaults.

    Returning floats (not e.g. ints) keeps the math inside the loop
    predictable: ``time.sleep(poll_seconds)`` and the idle comparison
    both treat the value as a wall-clock duration in seconds.

    A failure here is logged and falls back to defaults rather than
    crashing the watcher — the watcher is a long-lived background
    process and a transient DB hiccup must not kill it.
    """
    try:
        cfg = store.get_setting("ingest", {}) if store is not None else {}
    except Exception:
        cfg = {}
    idle = float(cfg.get("idle_seconds", DEFAULT_IDLE_SECONDS))
    poll = float(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS))
    return idle, poll


def run_watcher(
    loader: BaseLoader,
    watch_dir: Path,
    pipeline: MemoryPipeline,
    poll_seconds: float | None = None,
    idle_seconds: float | None = None,
    ledger: dict | None = None,
    on_ingest: callable | None = None,
    store: Any = None,
) -> None:
    """Watch a directory and ingest each transcript once it has been idle
    for ``idle_seconds``.

    ``ledger`` is a dict ``path → {mtime, size, ingested_at}`` used for
    idempotency. Pass in to share state across processes, leave None
    to use the default JSON file under ``watch_dir``.

    ``on_ingest`` is an optional callable invoked with no arguments
    after a successful ingest. The serve layer hooks this to a
    consolidator scheduler so ``realtime`` mode can fire.

    ``store`` is an optional :class:`MemoryStore`. When provided, the
    watcher reads ``ingest.idle_seconds`` / ``ingest.poll_seconds``
    from the settings table at every iteration so the user can dial
    ingest frequency from the Settings drawer WITHOUT restarting the
    launchd watcher process. Reads are throttled to once every
    ``SETTINGS_RELOAD_EVERY`` ticks (cheap SQLite SELECT) so we
    don't add noticeable overhead even at a 5-second poll cadence.
    """
    watch_dir = Path(watch_dir).expanduser()
    watch_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = _ledger_path(watch_dir)
    if ledger is None:
        ledger = _load_ledger(ledger_path)

    # Resolve initial values from the settings store if available,
    # otherwise fall back to the kwarg / module defaults.
    store_idle, store_poll = _read_ingest_settings(store)
    if idle_seconds is None:
        idle_seconds = store_idle
    if poll_seconds is None:
        poll_seconds = store_poll

    # Re-read settings on a wall-clock cadence instead of per-tick,
    # so reload latency doesn't grow with ``poll_seconds``. We default
    # to 30s: short enough that a user dialing the slider sees the
    # effect promptly, long enough that we don't hammer SQLite.
    SETTINGS_RELOAD_SECONDS = 30.0

    log.info(
        "watching %s for %s transcripts (idle>=%.0fs, poll=%.1fs)",
        watch_dir, loader.source, idle_seconds, poll_seconds,
    )

    def persist():
        _save_ledger(ledger_path, ledger)

    last_reload_at = 0.0
    try:
        while True:
            # Throttled settings reload on a wall-clock cadence so
            # the reload interval is stable regardless of poll_seconds.
            if store is not None:
                now_mono = time.monotonic()
                if now_mono - last_reload_at >= SETTINGS_RELOAD_SECONDS:
                    last_reload_at = now_mono
                    try:
                        new_idle, new_poll = _read_ingest_settings(store)
                        if new_idle != idle_seconds or new_poll != poll_seconds:
                            log.info(
                                "watcher settings reloaded: idle=%.0fs poll=%.1fs",
                                new_idle, new_poll,
                            )
                            idle_seconds = new_idle
                            poll_seconds = new_poll
                    except Exception:
                        log.exception("settings reload failed (using current values)")

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

                # Already-ingested but file changed.
                #
                # v2 fix (size-stable idle, not mtime-stable idle):
                # Previously any mtime refresh — including background
                # metadata flushes from Codex desktop that do not add
                # any new content — would reset the idle timer, which
                # meant a long-running active session would never
                # trigger an ingest: every keystroke flushed the file
                # mtime and we kept waiting.
                #
                # The real signal of "still being written" is *content
                # growth* (size increasing). mtime alone is unreliable.
                # We now track ``last_size_change_at`` and only treat a
                # file as active when its size is actually growing.
                if prev and prev.get("ingested_at"):
                    prev_size = prev.get("size", -1)
                    if st.st_size > prev_size:
                        # Real content growth → bump idle timestamp.
                        ledger[key] = {
                            "sig": list(sig),
                            "first_seen": prev.get("first_seen", now),
                            "last_mtime": st.st_mtime,
                            "size": st.st_size,
                            "last_size_change_at": now,
                            "ingested_at": None,
                        }
                    else:
                        # Only mtime refreshed, no new bytes. Keep the
                        # idle clock running — do NOT reset it.
                        ledger[key] = {
                            "sig": list(sig),
                            "first_seen": prev.get("first_seen", now),
                            "last_mtime": st.st_mtime,
                            "size": st.st_size,
                            # Fall back to the previous bump time so
                            # legacy ledgers without the field keep
                            # working.
                            "last_size_change_at": prev.get(
                                "last_size_change_at",
                                prev.get("first_seen", now),
                            ),
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
                        "last_size_change_at": now,
                        "ingested_at": None,
                    }
                    persist()
                    continue

                # Subsequent observation: only proceed if the file has
                # been size-stable (not just mtime-stable) for
                # ``idle_seconds``. Codex desktop touches mtime on
                # every flush but the size only grows when new
                # conversation content lands — that's the signal we
                # care about.
                last_change = ledger[key].get(
                    "last_size_change_at",
                    ledger[key].get("first_seen", now),
                )
                if (now - last_change) < idle_seconds:
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



def run_once(
    loader: BaseLoader,
    watch_dir: Path,
    pipeline: MemoryPipeline,
    poll_seconds: float = 2.0,
    idle_seconds: float = 0.0,
    ledger: dict | None = None,
    on_ingest: callable | None = None,
) -> dict[str, Any]:
    """Run a single ingest pass over ``watch_dir`` and return a summary.

    Unlike :func:`run_watcher` this does NOT loop — it scans once,
    ingests any file whose size has grown since the last successful
    ingest (or that has been size-stable for ``idle_seconds``), and
    returns. Used by the server-side force-ingest endpoint so a UI
    button can trigger one batch without spawning a long-lived
    watcher process.

    Returns a dict with::

        {
          "scanned": int,        # number of files seen
          "ingested": int,       # number of files successfully ingested
          "skipped": int,        # unchanged or already-ingested
          "errors": int,         # files that failed to load
          "files": [             # per-file detail
             {"path": str, "status": "ingested"|"skipped"|"error",
              "summary_items": int, "error": str?}
          ],
        }
    """
    watch_dir = Path(watch_dir).expanduser()
    watch_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = _ledger_path(watch_dir)
    if ledger is None:
        ledger = _load_ledger(ledger_path)

    def _persist():
        _save_ledger(ledger_path, ledger)

    result: dict[str, Any] = {
        "scanned": 0,
        "ingested": 0,
        "skipped": 0,
        "errors": 0,
        "files": [],
    }
    try:
        files = list(loader.discover(watch_dir))
    except FileNotFoundError:
        files = []

    now = time.time()
    for path in files:
        result["scanned"] += 1
        key = str(path)
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        if path.name == ".loop_memory_seen.json":
            continue

        prev = ledger.get(key)
        prev_size = (prev or {}).get("size", -1)
        prev_ingested = (prev or {}).get("ingested_at")

        # If the file is identical to what we last ingested, skip.
        if prev_ingested and st.st_size == prev_size:
            result["skipped"] += 1
            result["files"].append({"path": key, "status": "skipped"})
            continue

        # Optional idle gate: when idle_seconds > 0, only ingest if
        # the file's size has been stable for at least that long.
        # When idle_seconds == 0 (the default for run_once), ingest
        # immediately as long as new content exists.
        if idle_seconds > 0:
            last_change = (prev or {}).get(
                "last_size_change_at",
                (prev or {}).get("first_seen", now),
            )
            if (now - last_change) < idle_seconds:
                result["skipped"] += 1
                result["files"].append({
                    "path": key, "status": "skipped",
                    "reason": "not_idle_long_enough",
                })
                continue

        # Try to load + ingest.
        try:
            session = loader.load_one(path)
        except Exception as e:
            log.exception("loader failed on %s", path)
            result["errors"] += 1
            result["files"].append({
                "path": key, "status": "error",
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        if session is None:
            result["skipped"] += 1
            result["files"].append({"path": key, "status": "skipped"})
            continue

        try:
            pipe_result = pipeline.run(session)
            n_items = len(pipe_result.summary_items)
            result["ingested"] += 1
            result["files"].append({
                "path": key, "status": "ingested",
                "summary_items": n_items,
            })
            ledger[key] = {
                "sig": [st.st_mtime, st.st_size],
                "first_seen": (prev or {}).get("first_seen", now),
                "last_mtime": st.st_mtime,
                "size": st.st_size,
                "last_size_change_at": now,
                "ingested_at": now,
            }
            _persist()
            if on_ingest is not None:
                try:
                    on_ingest()
                except Exception:
                    log.exception("on_ingest callback failed")
        except Exception as e:
            log.exception("pipeline failed on %s", path)
            result["errors"] += 1
            result["files"].append({
                "path": key, "status": "error",
                "error": f"{type(e).__name__}: {e}",
            })

    return result
