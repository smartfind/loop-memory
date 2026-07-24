"""Background scheduler for LLM-driven consolidation.

A single daemon thread (started lazily on the first call) keeps a
cheap clock and triggers an ``LLMConsolidator.run`` when the user's
configured schedule says so. Modes supported:

* ``off``       - never run
* ``realtime``  - run after ingest has been idle for N seconds
* ``hourly``    - run every hour on the hour
* ``daily``     - run once a day at ``schedule.hour:schedule.minute``
* ``weekly``    - run once a week on ``schedule.weekday`` at the same
                  hour/minute
* ``interval``  - run every ``schedule.interval_minutes`` minutes

The scheduler is *co-operative*: it can be stopped and reconfigured
without restarting the server. ``tick(now)`` is idempotent.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from ..llm.providers import build_provider, default_config, validate_config
from ..storage.sqlite_store import MemoryStore
from .evolution import EvolutionConsolidator
from .llm_consolidate import ConsolidateStats, LLMConsolidator  # noqa: F401  (kept for backward-compat)

log = logging.getLogger(__name__)


def _next_daily(now: float, hour: int, minute: int) -> float:
    import datetime as _dt
    t = _dt.datetime.fromtimestamp(now)
    nxt = t.replace(hour=int(hour) % 24, minute=int(minute) % 60, second=0, microsecond=0)
    if nxt.timestamp() <= now:
        nxt = nxt + _dt.timedelta(days=1)
    return nxt.timestamp()


def _next_weekly(now: float, weekday: int, hour: int, minute: int) -> float:
    import datetime as _dt
    t = _dt.datetime.fromtimestamp(now)
    nxt = t.replace(hour=int(hour) % 24, minute=int(minute) % 60, second=0, microsecond=0)
    days_ahead = (int(weekday) - t.weekday()) % 7
    if days_ahead == 0 and nxt.timestamp() <= now:
        days_ahead = 7
    nxt = nxt + _dt.timedelta(days=days_ahead)
    return nxt.timestamp()


@dataclass
class _State:
    next_run: float = 0.0
    last_run: float = 0.0
    last_stats: dict[str, Any] | None = None
    last_error: str | None = None
    last_run_id: str | None = None
    is_running: bool = False
    last_ingest_at: float = 0.0    # updated by watcher / ingest hooks
    # Live progress for the currently active run.
    progress_current: int = 0
    progress_total: int = 0
    progress_started_at: float = 0.0
    progress_run_id: str | None = None
    progress_message: str = ""
    # Connectivity probe state — updated by /api/admin/llm/test and by
    # every successful real LLM call. Drives the top-bar model chip
    # dot color (green pulsing vs static vs amber vs red).
    last_test_ok: bool | None = None   # None = never tested
    last_test_at: float = 0.0          # epoch seconds
    last_test_message: str = ""
    # Compaction: small enough to share the same wake loop as the
    # LLM consolidator, but never blocks it.
    compact_running: bool = False
    last_compact_at: float = 0.0
    compact_started_at: float = 0.0
    compact_message: str = ""
    last_compact_report: dict[str, Any] | None = None


class ConsolidatorScheduler:
    """A small in-process scheduler.

    It is *not* a replacement for cron/launchd. It's good enough to
    satisfy "the page should auto-refresh on a schedule even when the
    server is just running in the background" - the typical developer
    loop.
    """

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._state = _State()
        self._thread: threading.Thread | None = None
        self._run_threads: set[threading.Thread] = set()
        self._stop = threading.Event()
        self._wake = threading.Event()  # set by config changes to recompute next_run
        self._cfg: dict[str, Any] = default_config()
        self._load_config()

    # --- public API -------------------------------------------------------

    def reload_config(self) -> dict[str, Any]:
        """Read latest config from the store; return the effective config."""
        with self._lock:
            self._load_config()
            self._recompute_next_run(time.time())
            self._wake.set()
        return self._cfg

    def status(self) -> dict[str, Any]:
        with self._lock:
            s = self._state
            cfg = self._cfg or {}
            # Check the secret backend for a stored API key. We do
            # this here so the top-bar chip can show the right state
            # without the frontend having to round-trip the keychain.
            try:
                from ..security import account_for, has_secret
                account = cfg.get("api_key_account") or account_for(cfg.get("provider") or "echo")
                key_set = has_secret(account)
            except Exception:
                key_set = False
            return {
                "is_running": s.is_running,
                "next_run": s.next_run if s.next_run > 0 else None,
                "last_run": s.last_run if s.last_run > 0 else None,
                "last_stats": s.last_stats,
                "last_error": s.last_error,
                "last_run_id": s.last_run_id,
                "schedule": (cfg.get("schedule") or {}),
                "behaviour": (cfg.get("behaviour") or {}),
                "provider": cfg.get("provider"),
                "model": cfg.get("model"),
                "api_key_set": bool(key_set),
                "api_key_fingerprint": cfg.get("api_key_fingerprint", "") or "",
                "last_test_ok": s.last_test_ok,
                "last_test_at": s.last_test_at if s.last_test_at > 0 else None,
                "last_test_message": s.last_test_message,
                "progress": {
                    "current": s.progress_current,
                    "total": s.progress_total,
                    "started_at": s.progress_started_at if s.progress_started_at > 0 else None,
                    "run_id": s.progress_run_id,
                    "message": s.progress_message,
                },
                "compact_running": s.compact_running,
                "last_compact_at": s.last_compact_at if s.last_compact_at > 0 else None,
                "compact_started_at": s.compact_started_at if s.compact_started_at > 0 else None,
                "compact_message": s.compact_message,
                "last_compact_report": s.last_compact_report,
            }

    def record_test_result(self, ok: bool, message: str = "") -> None:
        """Stash the result of a connectivity probe.

        Called by /api/admin/llm/test and any successful real LLM
        call (so the top-bar dot stays in sync without forcing the
        user to open the Settings drawer and click Test).
        """
        with self._lock:
            self._state.last_test_ok = bool(ok)
            self._state.last_test_at = time.time()
            self._state.last_test_message = (message or "")[:200]

    def notify_ingest(self) -> None:
        """Mark 'something was just ingested' so realtime mode can fire."""
        with self._lock:
            self._state.last_ingest_at = time.time()
            sched = self._cfg.get("schedule") or {}
            if sched.get("mode") == "realtime":
                self._recompute_next_run(time.time())
                self._wake.set()

    def run_now(self, trigger: str = "manual", block: bool = False) -> dict[str, Any] | None:
        """Run a consolidation pass synchronously (or on a background
        thread if ``block`` is False). Returns the run id when async."""
        if block:
            return self._do_run(trigger)
        self._start_run_thread(trigger)
        return None

    def run_blocking(self, label: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run an arbitrary callable off the request thread, returning
        the dict it produced. Used by maintenance endpoints
        (compaction, reindex, …) so the HTTP request returns quickly
        and the user can poll a status endpoint while the work
        completes.

        Returns ``{"status": "busy", ...}`` when a consolidation is
        already running, ``{"status": "error", ...}`` on failure,
        or ``{"status": "done", "label": label, "result": result}``
        on success. We delegate to a one-shot ``ThreadPoolExecutor``
        so the call returns quickly to FastAPI while the heavy
        work runs in a worker thread.
        """
        import concurrent.futures as _cf
        with self._lock:
            running = bool(self._state.is_running)
        if running:
            return {"status": "busy", "label": label, "detail": "another job is in progress"}
        with _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"lm-{label}") as ex:
            fut = ex.submit(fn)
            try:
                result = fut.result(timeout=300)
            except Exception as e:
                log.exception("%s job failed", label)
                return {"status": "error", "label": label, "error": str(e)}
        return {"status": "done", "label": label, "result": result}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="consolidator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            run_threads = list(self._run_threads)
        for thread in run_threads:
            thread.join(timeout=2.0)

    def _start_run_thread(self, trigger: str) -> None:
        def run() -> None:
            try:
                self._do_run_safe(trigger)
            finally:
                with self._lock:
                    self._run_threads.discard(thread)

        thread = threading.Thread(target=run, daemon=True)
        with self._lock:
            self._run_threads.add(thread)
        thread.start()

    # --- internals --------------------------------------------------------

    def _load_config(self) -> None:
        cfg = self.store.get_setting("llm_consolidator", default_config())
        if not isinstance(cfg, dict):
            cfg = default_config()
        cfg, _ = validate_config(cfg)
        self._cfg = cfg

    def _recompute_next_run(self, now: float) -> None:
        sched = self._cfg.get("schedule") or {}
        mode = sched.get("mode", "off")
        s = self._state
        if not sched.get("enabled", False) or mode == "off":
            s.next_run = 0.0
            return
        if mode == "hourly":
            # next top of hour
            import datetime as _dt
            t = _dt.datetime.fromtimestamp(now)
            nxt = t.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(hours=1)
            s.next_run = nxt.timestamp()
            return
        if mode == "daily":
            s.next_run = _next_daily(now, sched.get("hour", 3), sched.get("minute", 0))
            return
        if mode == "weekly":
            s.next_run = _next_weekly(
                now, sched.get("weekday", 0), sched.get("hour", 3), sched.get("minute", 0)
            )
            return
        if mode == "interval":
            minutes = max(1, int(sched.get("interval_minutes") or 60))
            base = s.last_run or now
            s.next_run = base + minutes * 60
            if s.next_run < now:
                s.next_run = now + 1
            return
        if mode == "realtime":
            idle = max(0, int(sched.get("after_ingest_idle_sec") or 30))
            base = max(s.last_ingest_at, now)
            s.next_run = base + idle
            return
        s.next_run = 0.0

    def _loop(self) -> None:
        log.info("consolidator scheduler started")
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                s = self._state
                sched = self._cfg.get("schedule") or {}
                enabled = bool(sched.get("enabled", False)) and (sched.get("mode", "off") != "off")
                if not enabled:
                    s.next_run = 0.0
                else:
                    if s.next_run <= 0:
                        self._recompute_next_run(now)
                    if now >= s.next_run and not s.is_running:
                        s.is_running = True
                        trigger = "schedule" if sched.get("mode") != "realtime" else "realtime"
                        self._start_run_thread(trigger)
                # Compaction runs on its own cadence so the user can
                # opt into background tidying without enabling the
                # expensive LLM consolidator. We piggy-back on the
                # same wake loop to avoid a second thread.
                self._maybe_schedule_compact(now)
                wait = max(1.0, min(60.0, (s.next_run - now) if s.next_run > 0 else 30.0))
            self._wake.wait(timeout=wait)
            self._wake.clear()
        log.info("consolidator scheduler stopped")

    # ---- compact cadence -------------------------------------------------

    def _maybe_schedule_compact(self, now: float) -> None:
        """Decide whether to kick off a compaction pass on the
        background thread. Cheap to call once per scheduler tick.

        Triggers:

          * ``auto_compact`` is on AND the cadence has elapsed
          * the store has crossed its byte budget
        """
        with self._lock:
            if self._state.is_running or self._state.compact_running:
                return
            cfg = self.store.get_setting("storage_budget", {}) or {}
        if not cfg:
            return
        auto = bool(cfg.get("auto_compact", False))
        interval_h = max(1, int(cfg.get("compact_interval_hours") or 24))
        last = (self.store.get_setting("last_compact", {}) or {}).get("finished_at", 0.0)
        max_bytes = int(cfg.get("max_bytes") or 0)
        size_now = self.store.db_size_bytes()
        budget_breached = max_bytes > 0 and size_now > max_bytes
        cadence_elapsed = (now - float(last)) >= interval_h * 3600
        if not (auto and cadence_elapsed) and not budget_breached:
            return
        reason = "budget" if budget_breached else "cadence"
        self._start_compact_thread(reason)

    def _start_compact_thread(self, reason: str) -> None:
        def run() -> None:
            try:
                self._do_compact(reason)
            finally:
                with self._lock:
                    self._state.compact_running = False
                    self._wake.set()

        with self._lock:
            if self._state.compact_running:
                return
            self._state.compact_running = True
        thread = threading.Thread(target=run, daemon=True, name=f"compactor-{reason}")
        thread.start()

    def _do_compact(self, reason: str) -> dict[str, Any]:
        from .compact import Compactor
        log.info("compaction start (reason=%s)", reason)
        with self._lock:
            self._state.compact_started_at = time.time()
            self._state.compact_message = "starting"

        def _progress(cur: int, total: int, msg: str) -> None:
            with self._lock:
                self._state.progress_current = cur
                self._state.progress_total = total
                self._state.compact_message = msg

        comp = Compactor(self.store, mode="heuristic")
        report = comp.run(progress=_progress, force=False)
        d = report.to_dict()
        d["reason"] = reason
        d["finished_at"] = time.time()
        self.store.set_setting("last_compact", d)
        with self._lock:
            self._state.last_compact_at = time.time()
            self._state.last_compact_report = d
            self._state.compact_message = ""
        log.info("compaction done: %s", d)
        return d

    def _do_run_safe(self, trigger: str) -> None:
        try:
            self._do_run(trigger)
        except Exception as e:
            log.exception("consolidator run failed: %s", e)
        finally:
            with self._lock:
                self._state.is_running = False
                self._recompute_next_run(time.time())
                self._wake.set()

    def _do_run(self, trigger: str) -> dict[str, Any]:
        # Reload config in case the user updated it between ticks.
        with self._lock:
            self._load_config()
            cfg = self._cfg
            model_name = cfg.get("model") or "?"
            self._state.is_running = True
            self._state.progress_current = 0
            self._state.progress_total = 0
            self._state.progress_started_at = time.time()
            self._state.progress_message = ""
        provider = build_provider(cfg)
        run_id = self.store.start_consolidation_run(trigger=trigger, model=model_name)
        log.info("consolidation run %s start (trigger=%s, model=%s)", run_id, trigger, model_name)
        with self._lock:
            self._state.progress_run_id = run_id

        def _progress(current: int, total: int) -> None:
            with self._lock:
                self._state.progress_current = current
                self._state.progress_total = total
                self._state.progress_message = f"{current}/{total} memories"

        stats: ConsolidateStats
        try:
            # Prefer the new EvolutionConsolidator: it adds memory dedup,
            # noisy wiki cleanup, and bullet-style wiki synthesis. Fall back
            # to the legacy single-pass LLMConsolidator if anything goes
            # wrong during construction (e.g. provider mismatch).
            try:
                cons = EvolutionConsolidator(self.store, provider, cfg.get("behaviour") or {})
                cons.set_run_id(run_id)
                stats = cons.run(progress=_progress)
            except Exception:
                log.warning("EvolutionConsolidator unavailable; falling back to LLMConsolidator", exc_info=True)
                cons = LLMConsolidator(self.store, provider, cfg.get("behaviour") or {})
                cons.set_run_id(run_id)
                stats = cons.run(progress=_progress)
        except Exception as e:
            log.exception("consolidator failed: %s", e)
            self.store.finish_consolidation_run(run_id, "error", stats=None, error=str(e))
            with self._lock:
                self._state.is_running = False
                self._state.progress_current = 0
                self._state.progress_total = 0
                self._state.progress_started_at = 0.0
                self._state.progress_message = ""
                self._state.last_error = str(e)
                self._state.last_run_id = run_id
                self._state.last_run = time.time()
                # If the run blew up while talking to the provider,
                # remember it so the top-bar dot goes amber on the
                # next page load.
                if any(s in str(e).lower() for s in ("llm", "provider", "api", "http")):
                    self._state.last_test_ok = False
                    self._state.last_test_at = time.time()
                    self._state.last_test_message = str(e)[:200]
            return {"run_id": run_id, "status": "error", "error": str(e)}
        d = stats.to_dict()
        self.store.finish_consolidation_run(run_id, "done", stats=d)
        # The run successfully talked to the provider — promote the
        # top-bar dot from "set but stale" to "verified reachable".
        with self._lock:
            self._state.last_test_ok = True
            self._state.last_test_at = time.time()
            self._state.last_test_message = "live run succeeded"
        # If the run produced or updated any wiki pages, refresh the
        # knowledge graph from the new distilled knowledge so the
        # graph tab stays in sync with the wiki.
        try:
            # Tolerate both EvolutionStats (wiki_created / wiki_updated)
            # and legacy LLMConsolidator.stats (wiki_pages_created / wiki_pages_updated).
            wpc = (d.get("wiki_pages_created") or 0) + (d.get("wiki_created") or 0)
            wpu = (d.get("wiki_pages_updated") or 0) + (d.get("wiki_updated") or 0)
            if wpc + wpu > 0:
                from ..graph.build import KnowledgeGraph
                report = KnowledgeGraph(self.store).rebuild_from_wiki(clear=True)
                log.info(
                    "graph auto-rebuilt after run %s: %d entities, %d relations",
                    run_id, report.entities, report.relations,
                )
        except Exception as e:
            log.warning("post-run graph rebuild failed: %s", e)
        with self._lock:
            self._state.is_running = False
            self._state.progress_current = 0
            self._state.progress_total = 0
            self._state.progress_started_at = 0.0
            self._state.progress_message = ""
            self._state.last_run = time.time()
            self._state.last_stats = d
            self._state.last_error = None
            self._state.last_run_id = run_id
        log.info("consolidation run %s done: %s", run_id, d)
        return {"run_id": run_id, "status": "done", "stats": d}
