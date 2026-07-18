from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, HTTPException

from loop_memory.serve.handlers import (
    PIPELINE_STAGES,
    memory_to_dict,
    pipeline_dashboard,
    pipeline_stage_items,
    require_scheduler,
    session_to_dict,
)
from loop_memory.storage.sqlite_store import MemoryStore


def _store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-mem-handlers-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class _FakeMemory:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class PipelineDashboardTests(unittest.TestCase):
    def test_returns_all_six_stages_even_when_empty(self) -> None:
        store, tmp = _store()
        try:
            out = pipeline_dashboard(store)
            self.assertEqual([s["stage"] for s in out["stages"]], list(PIPELINE_STAGES))
            # totals keys always present
            for k in ("memories", "wiki_pages", "wiki_avg_importance", "avg_score"):
                self.assertIn(k, out["totals"])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stage_items_empty_when_no_runs(self) -> None:
        store, tmp = _store()
        try:
            out = pipeline_stage_items(store, "ingest")
            self.assertEqual(out["run"], None)
            self.assertEqual(out["items"], [])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stage_items_includes_run_metadata(self) -> None:
        store, tmp = _store()
        try:
            run_id = store.start_pipeline_run("ingest")
            store.finish_pipeline_run(run_id, in_count=3, out_count=5,
                                      note="seed run",
                                      stats={"evidence_ids": ["x1", "x2"]})
            out = pipeline_stage_items(store, "ingest")
            self.assertIsNotNone(out["run"])
            self.assertEqual(out["run"]["in_count"], 3)
            self.assertEqual(out["run"]["out_count"], 5)
            self.assertEqual(out["run"]["note"], "seed run")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stage_items_returns_empty_when_no_runs(self) -> None:
        # When there is no pipeline run for the stage, the handler
        # returns run=None and items=[] (we don't fall back to
        # list_memories here because that would mask the real signal
        # the user is looking for).
        store, tmp = _store()
        try:
            store.upsert_memory(kind="fact", text="alpha", importance=0.5, source="test")
            store.upsert_memory(kind="fact", text="beta", importance=0.5, source="test")
            out = pipeline_stage_items(store, "wiki", limit=10)
            self.assertEqual(out["run"], None)
            self.assertEqual(out["items"], [])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stage_items_resolves_evidence_ids(self) -> None:
        store, tmp = _store()
        try:
            created = store.upsert_memory(kind="fact", text="alpha", importance=0.5, source="test")
            run_id = store.start_pipeline_run("distill")
            store.finish_pipeline_run(run_id, in_count=1, out_count=1,
                                      stats={"evidence_ids": [created.id]})
            out = pipeline_stage_items(store, "distill")
            self.assertEqual(len(out["items"]), 1)
            self.assertEqual(out["items"][0]["id"], created.id)
            self.assertEqual(out["items"][0]["kind"], "fact")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_stats_json_does_not_crash(self) -> None:
        store, tmp = _store()
        try:
            run_id = store.start_pipeline_run("wiki")
            # write a broken stats_json directly
            with store._conn() as c:
                c.execute("UPDATE pipeline_runs SET stats_json=? WHERE id=?",
                          ("not-json", run_id))
            # Should not raise — handlers fall back to {}
            out = pipeline_stage_items(store, "wiki")
            self.assertEqual(out["items"], [])
            self.assertEqual(out["run"]["stats"], {})
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class RequireSchedulerTests(unittest.TestCase):
    def test_raises_503_when_missing(self) -> None:
        app = FastAPI()
        with self.assertRaises(HTTPException) as ctx:
            require_scheduler(app)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_returns_scheduler_when_present(self) -> None:
        app = FastAPI()
        sentinel = object()
        app.state.scheduler = sentinel
        self.assertIs(require_scheduler(app), sentinel)


class FormatterTests(unittest.TestCase):
    def test_memory_to_dict_uses_dot_rounding(self) -> None:
        m = _FakeMemory(
            id="x", kind="fact", text="hi", importance=0.123456,
            score=0.987654, source="codex", session_id="s",
            created_at=100.0, updated_at=200.0, tags=["a", "b"],
        )
        d = memory_to_dict(m)
        self.assertEqual(d["importance"], 0.123)
        self.assertEqual(d["score"], 0.9877)
        self.assertEqual(d["tags"], ["a", "b"])

    def test_memory_to_dict_falls_back_to_created_for_updated(self) -> None:
        m = _FakeMemory(
            id="x", kind="fact", text="hi", importance=0.5, score=0.5,
            source="codex", session_id="s", created_at=1.0, tags=[],
        )
        # no updated_at attr
        d = memory_to_dict(m)
        self.assertEqual(d["updated_at"], 1.0)

    def test_session_to_dict_round_trip(self) -> None:
        s = _FakeMemory(
            id="s", source="codex", external_id="e", title="t",
            started_at=1.0, ended_at=2.0, message_count=5,
        )
        d = session_to_dict(s)
        self.assertEqual(d, {
            "id": "s", "source": "codex", "external_id": "e",
            "title": "t", "started_at": 1.0, "ended_at": 2.0,
            "message_count": 5,
        })


if __name__ == "__main__":
    unittest.main()
