"""``latest_only`` recall mode (audit 2026-10-25, loop-memory 0.4.16).

Mem0 v2 ships ``SearchMemoryOptions.latest_only`` / ``GetAllMemoryOptions.latest_only``
so a caller can ask for "the current answer" instead of "every version
of the answer". loop-memory implements the same idea in three layers:

1. **current truth only** -- rows whose validity window does not
   contain *now* are dropped;
2. **subject-key collapse** -- a logical slot resolves to its newest
   live row, looked up in the store (not just in the candidate list);
3. **near-duplicate collapse** -- paraphrases collapse to the newest.

The default behaviour is unchanged: ``latest_only=False`` keeps the
legacy "everything" payload, which the 0.4.15 validity tests pin.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from loop_memory.storage.sqlite_store import MemoryStore


class LatestOnlyRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = MemoryStore(os.path.join(self.tmp, "latest.db"))
        self.now = time.time()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _texts(self, result) -> list[str]:
        return [m["text"] for m in result["memories"]]

    # --- rule 1: current truth only ---------------------------------

    def test_expired_row_is_dropped(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        r = self.store.recall("deploy pipeline", limit=10, latest_only=True)
        self.assertEqual(self._texts(r), ["the deploy pipeline runs on gitlab ci"])

    def test_future_row_is_dropped(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            valid_at=self.now + 3600,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        r = self.store.recall("deploy pipeline", limit=10, latest_only=True)
        self.assertEqual(self._texts(r), ["the deploy pipeline runs on gitlab ci"])

    def test_default_recall_still_returns_expired_rows(self) -> None:
        """The 0.4.15 contract is unchanged: the validity filter is
        opt-in, so plain recall keeps returning everything."""
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        r = self.store.recall("deploy pipeline", limit=10)
        self.assertEqual(len(self._texts(r)), 2)

    # --- rule 2: subject-key collapse --------------------------------

    def test_subject_key_slot_returns_only_the_newest(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the editor preference is tabs",
            agent_id="c", user_id="u", subject_key="editor.preference",
            created_at=self.now - 100, updated_at=self.now - 100,
        )
        self.store.upsert_memory(
            kind="fact", text="the editor preference is spaces",
            agent_id="c", user_id="u", subject_key="editor.preference",
            created_at=self.now, updated_at=self.now,
        )
        r = self.store.recall("editor preference", limit=10, latest_only=True)
        self.assertEqual(self._texts(r), ["the editor preference is spaces"])

    def test_stale_version_is_dropped_even_when_it_matches_better(self) -> None:
        """A superseded version must not come back just because it
        scored higher on the query -- the slot has one answer."""
        self.store.upsert_memory(
            kind="fact", text="editor preference tabs vim emacs",
            agent_id="c", user_id="u", subject_key="editor.preference",
            importance=0.9, created_at=self.now - 100, updated_at=self.now - 100,
        )
        self.store.upsert_memory(
            kind="fact", text="editor preference spaces",
            agent_id="c", user_id="u", subject_key="editor.preference",
            importance=0.5, created_at=self.now, updated_at=self.now,
        )
        r = self.store.recall("editor preference", limit=10, latest_only=True)
        self.assertEqual(self._texts(r), ["editor preference spaces"])

    def test_different_slots_are_not_collapsed(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="editor preference is tabs",
            agent_id="c", user_id="u", subject_key="editor.preference",
        )
        self.store.upsert_memory(
            kind="fact", text="shell preference is fish",
            agent_id="c", user_id="u", subject_key="shell.preference",
        )
        r = self.store.recall("preference is", limit=10, latest_only=True)
        self.assertEqual(len(self._texts(r)), 2)

    # --- rule 3: near-duplicate collapse -----------------------------

    def test_near_duplicate_texts_collapse_to_the_newest(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request",
            created_at=self.now - 100, updated_at=self.now - 100,
        )
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request again",
            created_at=self.now, updated_at=self.now,
        )
        r = self.store.recall("api returns 500", limit=10, latest_only=True)
        self.assertEqual(len(self._texts(r)), 1)
        self.assertIn("again", self._texts(r)[0])

    def test_distinct_facts_are_not_collapsed(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on staging",
        )
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on production",
        )
        r = self.store.recall("api returns 500", limit=10, latest_only=True)
        self.assertEqual(len(self._texts(r)), 2)

    # --- plumbing -----------------------------------------------------

    def test_hybrid_path_supports_latest_only(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on github actions",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on gitlab ci",
        )
        r = self.store.recall_hybrid("deploy pipeline", limit=10, latest_only=True)
        self.assertEqual(self._texts(r), ["the deploy pipeline runs on gitlab ci"])

    def test_default_paths_are_unchanged(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request",
            invalid_at=self.now - 10,
        )
        self.store.upsert_memory(
            kind="fact", text="the api returns 500 on every request again",
        )
        r = self.store.recall("api returns 500", limit=10)
        self.assertEqual(len(self._texts(r)), 2)
        r = self.store.recall_hybrid("api returns 500", limit=10)
        self.assertEqual(len(self._texts(r)), 2)

    def test_latest_only_survives_the_char_budget(self) -> None:
        """The collapse runs *before* the payload budget so a stale row
        cannot eat the caller's character allowance."""
        for i in range(6):
            self.store.upsert_memory(
                kind="fact", text=f"the deploy pipeline runs on runner {i}",
                invalid_at=self.now - 10,
            )
        self.store.upsert_memory(
            kind="fact", text="the deploy pipeline runs on the new runner",
        )
        r = self.store.recall("deploy pipeline", limit=10, latest_only=True,
                              max_chars=200)
        self.assertEqual(len(self._texts(r)), 1)

    def test_recall_rows_expose_subject_key(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="editor preference is tabs",
            agent_id="c", user_id="u", subject_key="editor.preference",
        )
        r = self.store.recall("editor preference", limit=10)
        self.assertEqual(r["memories"][0].get("subject_key"), "editor.preference")

    def test_hybrid_rows_expose_subject_key(self) -> None:
        self.store.upsert_memory(
            kind="fact", text="editor preference is tabs",
            agent_id="c", user_id="u", subject_key="editor.preference",
        )
        r = self.store.recall_hybrid("editor preference", limit=10)
        self.assertEqual(r["memories"][0].get("subject_key"), "editor.preference")

    def test_empty_query_is_safe(self) -> None:
        r = self.store.recall("", limit=10, latest_only=True)
        self.assertEqual(r["memories"], [])


if __name__ == "__main__":
    unittest.main()
