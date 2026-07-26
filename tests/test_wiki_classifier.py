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
        self.assertFalse(result.auto_global)
        self.assertEqual(result.kind, "security-incident")

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
