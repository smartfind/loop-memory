"""Tests for automatic wiki security classification and scope routing."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.jobs.llm_consolidate import LLMConsolidator
from loop_memory.llm.providers import RuleBasedProvider
from loop_memory.storage.sqlite_store import MemoryStore
from loop_memory.wiki import classify_page, derive_default_scope


class WikiClassifierUnitTests(unittest.TestCase):
    def test_security_best_practices_are_global_candidates(self) -> None:
        examples = (
            "Rotate API keys quarterly",
            "Never paste secrets in chat",
            "Use parameterized SQL queries",
            "Use parametrised SQL",
            "使用参数化 SQL 查询，避免注入攻击",
        )
        for text in examples:
            with self.subTest(text=text):
                result = classify_page(title=text, body=text)
                self.assertTrue(result.is_security)
                self.assertTrue(result.auto_global)
                self.assertEqual(result.scope_hint, "global")

    def test_personal_or_instance_knowledge_stays_source_scoped(self) -> None:
        examples = (
            "I prefer Claude to Codex",
            "My wife's birthday 1990-01-01",
            "Codex desktop bug #1234",
            "My API key is only for this project",
            "Never paste secrets: api_key=sk-12345678901234567890",
        )
        for text in examples:
            with self.subTest(text=text):
                result = classify_page(title=text, body=text)
                self.assertFalse(result.auto_global)
                self.assertEqual(result.scope_hint, "per-source")

    def test_incident_without_general_rule_is_not_global(self) -> None:
        result = classify_page(body="Security incident CVE-2024-12345 affected one host")
        self.assertTrue(result.is_security)

    def test_english_substring_false_positives_are_not_security(self) -> None:
        """Audit H4: the previous security regex set had no
        \b word boundaries, so pages containing the substrings
        `rce` (inside "source"/"requirement"/"requires"),
        `ssrf`, `xss`, or `secret` without those substrings
        being actual security terms triggered a false
        `is_security=True` classification. The legacy wiki had ~15
        "global" pages that were promoted under v7's default-to-
        global rule, and the lack of boundaries would have let the
        same noise leak into new writes. This test locks the
        fix in place."""
        benign = (
            "Source of truth for the dashboard chart",
            "Requirements: project must follow open source standards",
            "This repo stores its settings in SQLite",
            "Use a hash table to deduplicate values before storing",
            "Hash the user id into a 32-bit integer for the cache key",
        )
        for text in benign:
            with self.subTest(text=text):
                result = classify_page(title=text, body=text)
                self.assertFalse(result.is_security,
                                 f"benign text falsely classified as security: {text!r}")
                self.assertFalse(result.auto_global)
                self.assertEqual(result.scope_hint, "per-source")

    def test_default_scope_uses_evidence_source_without_global_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "db.sqlite")
            memory = store.upsert_memory(
                kind="fact",
                text="A client-specific preference",
                source="claude",
                importance=0.8,
            )
            self.assertEqual(
                derive_default_scope(evidence_ids=[memory.id], store=store),
                "claude",
            )
            self.assertEqual(
                derive_default_scope(source_hint="open-claw", store=store),
                "openclaw",
            )
            self.assertEqual(derive_default_scope(store=store), "codex")

    def test_storage_write_path_applies_auto_scope_when_scope_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "db.sqlite")
            memory = store.upsert_memory(
                kind="fact",
                text="A Claude-only preference",
                source="claude",
                importance=0.8,
            )
            page = store.upsert_wiki_page(
                slug="storage-default",
                title="A Claude-only preference",
                body="A Claude-only preference",
                evidence_ids=[memory.id],
            )
            self.assertEqual(page["scope"], "claude")
            self.assertIsNotNone(page["auto_classification"])


class WikiScopeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.directory.name) / "db.sqlite")
        self.client = TestClient(create_app(self.store))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _create(self, **payload):
        response = self.client.post("/api/wiki", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_security_page_is_promoted_and_audited(self) -> None:
        page = self._create(
            slug="rotate-keys",
            title="Rotate API keys quarterly",
            body="Rotate API keys quarterly.",
        )
        self.assertEqual(page["scope"], "global")
        audit = page["auto_classification"]
        self.assertEqual(audit["scope_decision"], "auto-global")
        self.assertTrue(audit["auto_global"])
        self.assertEqual(audit["scope_applied"], "global")

        history = self.client.get(f"/api/wiki/{page['id']}/classification-history")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()["history"]), 1)

    def test_non_security_page_defaults_to_requested_client(self) -> None:
        page = self._create(
            slug="claude-preference",
            title="I prefer Claude to Codex",
            body="I prefer Claude to Codex for this workflow.",
            source="claude",
        )
        self.assertEqual(page["scope"], "claude")
        self.assertEqual(page["auto_classification"]["scope_decision"], "default-source")

    def test_explicit_scope_wins_over_classifier(self) -> None:
        page = self._create(
            slug="explicit-client",
            title="Never paste secrets in chat",
            body="Never paste secrets in chat.",
            scope="hermes",
        )
        self.assertEqual(page["scope"], "hermes")
        self.assertEqual(page["auto_classification"]["scope_decision"], "explicit")
        self.assertTrue(page["auto_classification"]["auto_global"])

    def test_auto_scope_can_be_disabled_without_making_pages_global(self) -> None:
        response = self.client.put(
            "/api/admin/wiki/scope",
            json={"enabled": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        page = self._create(
            slug="disabled-security",
            title="Never paste secrets in chat",
            body="Never paste secrets in chat.",
            source="claude",
        )
        self.assertEqual(page["scope"], "claude")
        self.assertFalse(page["auto_classification"]["auto_global"])

    def test_preview_does_not_persist_and_returns_recommendation(self) -> None:
        response = self.client.post(
            "/api/wiki/classify",
            json={"title": "Use parameterized SQL", "body": "Use parameterized SQL queries."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["recommended_scope"], "global")
        self.assertEqual(self.store.count_wiki_pages(), 0)

    def test_update_without_scope_preserves_legacy_scope(self) -> None:
        original = self.store.upsert_wiki_page(
            slug="legacy",
            title="Legacy page",
            body="A manually scoped page",
            scope="claude",
        )
        response = self.client.put(
            f"/api/wiki/{original['id']}",
            json={"title": "Never paste secrets in chat", "body": "Never paste secrets in chat."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["scope"], "claude")
        self.assertEqual(response.json()["auto_classification"]["scope_decision"], "preserved-existing")

    def test_bulk_scope_is_recorded_as_manual_override(self) -> None:
        page = self._create(
            slug="bulk-target",
            title="Rotate API keys quarterly",
            body="Rotate API keys quarterly.",
        )
        response = self.client.post(
            "/api/wiki/bulk-scope",
            json={"scope": "claude", "page_ids": [page["id"]]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        updated = self.store.get_wiki_page(page["id"])
        self.assertEqual(updated["scope"], "claude")
        self.assertEqual(updated["auto_classification"]["scope_decision"], "manual-override")

    def test_legacy_recall_honours_client_scope(self) -> None:
        self.store.upsert_wiki_page(
            slug="legacy-codex",
            title="scope-regression-marker",
            body="scope-regression-marker",
            scope="codex",
        )
        self.store.upsert_wiki_page(
            slug="legacy-claude",
            title="scope-regression-marker",
            body="scope-regression-marker",
            scope="claude",
        )
        response = self.client.get(
            "/api/recall",
            params={"query": "scope-regression-marker", "source": "codex", "mode": "legacy"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        titles = {row["title"] for row in response.json()["wiki"]}
        self.assertIn("scope-regression-marker", titles)
        self.assertEqual(len(response.json()["wiki"]), 1)


class WikiConsolidationScopeTests(unittest.TestCase):
    def test_rule_based_distillation_promotes_only_universal_security(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "db.sqlite")
            security = store.upsert_memory(
                kind="fact",
                text="Never paste API secrets in chat; use a secret store instead.",
                source="claude",
                importance=0.9,
            )
            preference = store.upsert_memory(
                kind="preference",
                text="The user prefers concise answers from Claude in this project.",
                source="claude",
                importance=0.8,
            )
            consolidator = LLMConsolidator(
                store,
                RuleBasedProvider(),
                {
                    "enable_filter": False,
                    "enable_score": False,
                    "enable_summarize": False,
                    "enable_wiki": True,
                    "batch_size": 20,
                },
            )
            consolidator.run(memories=[security, preference])
            pages = store.list_wiki_pages(limit=20)
            by_title = {page["title"]: page for page in pages}
            security_page = by_title["已提炼的事实 (Facts)"]
            preference_page = by_title["用户偏好 (Preferences)"]
            self.assertEqual(security_page["scope"], "global")
            self.assertEqual(preference_page["scope"], "claude")


if __name__ == "__main__":
    unittest.main()


class WikiLegacyBackfillTests(unittest.TestCase):
    """Audit H5: legacy wiki pages (created before the per-source
    classifier) were left at scope='global' regardless of content.
    The backfill in ``loop_memory.wiki.reclassify_legacy_pages``
    re-runs the classifier and downgrades non-universal-security
    rows to per-source scope. Without these tests a future
    classifier relax could let unrelated content linger in the
    global pool forever."""

    def _legacy_global(self, store, title, body):
        """Insert a wiki page that mimics a pre-classifier v7 row:
        scope=global plus an auto_classification=NULL so the
        new pipeline treats it as legacy content that has never been
        audited. The store's own upsert helper can't produce this
        state, so we're forced to write it directly via the
        underlying connection."""
        import uuid as _uuid
        import time as _time
        slug = title.lower().replace(" ", "-")
        with store._conn() as c:
            c.execute(
                "INSERT INTO wiki_pages(id, slug, title, body, summary, tags, "
                "importance, evidence_ids, version, created_at, updated_at, scope, "
                "auto_classification) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (_uuid.uuid4().hex, slug, title, body, "",
                 "[]", 0.5, "[]", 1,
                 _time.time(), _time.time(), "global"),
            )
        return store.get_wiki_page_by_slug(slug)

    def test_backfill_downgrades_unrelated_global_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "db.sqlite")
            from loop_memory.wiki import reclassify_legacy_pages
            self._legacy_global(store, "Chat2BI parsing plan",
                                "PDF / DOCX / XLSX / PPTX parsing pipeline")
            self._legacy_global(store, "WebSocket session sync decision",
                                "WebSocket opens on session change in ChatView")
            # One row that should genuinely stay global:
            self._legacy_global(store,
                                "Never paste API keys in chat",
                                "Avoid pasting secrets in conversation history. "
                                "Use environment variables instead.")
            summary = reclassify_legacy_pages(store)
            self.assertEqual(summary["legacy_global"], 3)
            self.assertEqual(summary["kept_global"], 1)
            self.assertEqual(summary["downgraded"], 2)
            kept = [
                p for p in store.list_wiki_pages(limit=20)
                if p["scope"] == "global"
            ]
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["slug"],
                              "never-paste-api-keys-in-chat")
            # Downgraded rows carry an audit with the legacy anchor.
            for p in store.list_wiki_pages(limit=20):
                if p["scope"] == "global":
                    continue
                ac = p.get("auto_classification") or {}
                self.assertEqual(ac.get("scope_decision"), "legacy-backfill")
                hist = ac.get("history") or []
                self.assertTrue(any(h.get("at_backfill") for h in hist),
                                f"missing backfill history anchor on {p['slug']}")

    def test_backfill_is_noop_for_pages_with_audit(self) -> None:
        """Pages whose ``auto_classification`` is already set went
        through the new pipeline and must NOT be touched, even when
        their scope is global."""
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "db.sqlite")
            # Page whose auto_classification was set by an earlier
            # auto-route (e.g. ``legacy-backfill`` had already
            # downgraded once). Re-running the backfill should be
            # idempotent.
            from loop_memory.wiki import reclassify_legacy_pages
            store.upsert_wiki_page(
                slug="auto-routed-security",
                title="Use parameterised SQL",
                body="Use parameterised SQL to avoid injection.",
                summary="",
                tags=[],
                importance=0.7,
                scope="global",
                auto_classification={"scope_decision": "auto-global",
                                      "scope_applied": "global"},
            )
            summary = reclassify_legacy_pages(store)
            self.assertEqual(summary["legacy_global"], 0)
            self.assertEqual(summary["downgraded"], 0)

    def test_backfill_helper_exposed_via_cli(self) -> None:
        from loop_memory.cli.main import COMMANDS
        self.assertIn("wiki-reclassify-legacy", COMMANDS)
