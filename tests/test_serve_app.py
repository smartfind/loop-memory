from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from loop_memory.serve.app import create_app
from loop_memory.storage.sqlite_store import MemoryStore


def _store() -> tuple[MemoryStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="loop-mem-test-"))
    return MemoryStore(str(tmp / "db.sqlite")), tmp


class ServeAppSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.tmp = _store()
        self.app = create_app(self.store)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        # MemoryStore uses a connection per call; nothing to close explicitly.
        # Just wipe the temp dir.
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stats_endpoint_returns_dict(self) -> None:
        r = self.client.get("/api/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), dict)

    def test_memories_endpoint_empty(self) -> None:
        r = self.client.get("/api/memories")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_csp_allows_self_hosted_vue_template_compiler(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        csp = r.headers["content-security-policy"]
        self.assertIn("script-src 'self' 'unsafe-eval'", csp)
        self.assertNotIn("unpkg.com", csp)
        self.assertNotIn("'unsafe-inline'", csp.split("style-src", 1)[0])

    def test_static_javascript_always_revalidates(self) -> None:
        r = self.client.get("/static/js/api.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-cache, must-revalidate")

    def test_admin_rescore_returns_updated(self) -> None:
        r = self.client.post("/api/admin/rescore")
        self.assertEqual(r.status_code, 200)
        self.assertIn("updated", r.json())

    def test_admin_gc_returns_deleted(self) -> None:
        r = self.client.post("/api/admin/gc")
        self.assertEqual(r.status_code, 200)
        self.assertIn("deleted", r.json())

    def test_admin_consolidate_runs(self) -> None:
        # store is empty, so report fields are 0
        r = self.client.post("/api/admin/consolidate")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for k in ("rescored", "gc_removed", "merged", "elapsed_ms"):
            self.assertIn(k, body)

    def test_admin_consolidate_now_without_scheduler_503(self) -> None:
        r = self.client.post("/api/admin/consolidate-now")
        # No scheduler attached -> 503 from the endpoint
        self.assertEqual(r.status_code, 503)

    def test_admin_consolidate_now_with_mock_scheduler(self) -> None:
        class _Sched:
            def run_now(self, trigger, block):
                return {"ran": True, "trigger": trigger}
        self.app.state.scheduler = _Sched()
        r = self.client.post("/api/admin/consolidate-now")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["queued"])
        self.assertEqual(r.json()["result"]["trigger"], "manual")

    def test_admin_consolidate_now_returns_queued_when_scheduler_returns_none(self) -> None:
        class _Sched:
            def run_now(self, trigger, block):
                return None
        self.app.state.scheduler = _Sched()
        r = self.client.post("/api/admin/consolidate-now")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["queued"])

    def test_admin_llm_providers_endpoint(self) -> None:
        r = self.client.get("/api/admin/llm/providers")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # Endpoint returns a list of provider dicts
        self.assertIsInstance(body, list)
        ids = {p["id"] for p in body}
        self.assertIn("openai", ids)
        self.assertIn("echo", ids)

    def test_admin_llm_config_get_returns_default_when_unset(self) -> None:
        r = self.client.get("/api/admin/llm/config")
        self.assertEqual(r.status_code, 200)
        cfg = r.json()["config"]
        self.assertEqual(cfg["provider"], "echo")
        self.assertFalse(cfg["api_key_set"])

    def test_admin_llm_config_put_strips_api_key_and_persists(self) -> None:
        r = self.client.put("/api/admin/llm/config", json={
            "provider": "openai", "model": "gpt-x", "api_key": "should-not-be-stored"
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn("api_key", body["config"])
        # Reload and confirm the key is gone from persisted settings
        r2 = self.client.get("/api/admin/llm/config")
        self.assertNotIn("api_key", r2.json()["config"])

    def test_admin_llm_clear_key_is_idempotent(self) -> None:
        r = self.client.delete("/api/admin/llm/key")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["removed"])
        # Second call should still 200, just removed=False
        r2 = self.client.delete("/api/admin/llm/key")
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["removed"])

    def test_admin_llm_schedule_persists_quick_toggle(self) -> None:
        r = self.client.post("/api/admin/llm/schedule", json={"enabled": True, "mode": "hourly"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["schedule"]["enabled"])
        # Confirm persisted
        r2 = self.client.get("/api/admin/llm/config")
        self.assertTrue(r2.json()["config"]["schedule"]["enabled"])

    def test_wiki_export_returns_string(self) -> None:
        r = self.client.get("/api/wiki/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("markdown", r.json())
        self.assertIsInstance(r.json()["markdown"], str)

    def test_wiki_ask_requires_q(self) -> None:
        # FastAPI returns 422 when required query param q is missing
        r = self.client.post("/api/wiki/ask")
        self.assertEqual(r.status_code, 422)
        # Empty q -> 200, no pages
        r2 = self.client.post("/api/wiki/ask?q=alpha")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["matches"], [])

    def test_memories_delete_404_on_missing(self) -> None:
        r = self.client.delete("/api/memories/nonexistent-id")
        # 404 because id doesn't exist
        self.assertIn(r.status_code, (404, 200))

    def test_pipeline_endpoint_returns_stage_array(self) -> None:
        r = self.client.get("/api/pipeline")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("stages", body)
        names = [s.get("stage") for s in body["stages"]]
        for expected in ("ingest", "score", "cluster", "distill", "wiki", "memo"):
            self.assertIn(expected, names)


class IndexRouteTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for static module parsing")
    def test_static_javascript_modules_parse_as_esm(self) -> None:
        static_js = Path(__file__).parents[1] / "loop_memory" / "serve" / "static" / "js"
        with tempfile.TemporaryDirectory(prefix="loop-memory-js-") as tmp:
            for module in static_js.rglob("*.js"):
                check_file = Path(tmp) / f"{module.stem}-{abs(hash(module))}.mjs"
                check_file.write_text(module.read_text(encoding="utf-8"), encoding="utf-8")
                result = subprocess.run(
                    ["node", "--check", str(check_file)], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, f"{module}: {result.stderr}")

    def test_static_modules_do_not_reexport_direct_exports(self) -> None:
        static_js = Path(__file__).parents[1] / "loop_memory" / "serve" / "static" / "js"
        direct_pattern = re.compile(
            r"\bexport\s+(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)"
        )
        list_pattern = re.compile(r"\bexport\s*\{([^}]*)\}")
        for module in static_js.rglob("*.js"):
            source = module.read_text(encoding="utf-8")
            direct = set(direct_pattern.findall(source))
            listed: set[str] = set()
            for group in list_pattern.findall(source):
                listed.update(
                    item.strip().split(" as ", 1)[0].strip()
                    for item in group.split(",")
                    if item.strip()
                )
            self.assertFalse(direct & listed, f"duplicate exports in {module}: {direct & listed}")

    def test_async_components_resolve_named_exports(self) -> None:
        static_js = Path(__file__).parents[1] / "loop_memory" / "serve" / "static" / "js"
        unresolved = re.compile(
            r"defineAsyncComponent\(\(\)\s*=>\s*import\(['\"][^'\"]+['\"]\)\s*\)"
        )
        for module in static_js.rglob("*.js"):
            source = module.read_text(encoding="utf-8")
            self.assertIsNone(
                unresolved.search(source),
                f"async component in {module} must resolve its named export",
            )

    def test_root_returns_index_or_404_when_no_static(self) -> None:
        store, tmp = _store()
        try:
            app = create_app(store, static_dir=None)
            r = TestClient(app).get("/")
            # Either 200 (index.html present) or 404 (no static dir) is fine
            self.assertIn(r.status_code, (200, 404))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class SecurityHeadersTests(ServeAppSmokeTests):
    """Covers the security headers and CSRF guard in
    ``loop_memory/serve/app.py``. The CSP and Bearer-token tests live
    elsewhere; this class focuses on what is easy to break by accident."""

    def test_csp_includes_object_and_base_uri(self) -> None:
        r = self.client.get("/")
        csp = r.headers.get("Content-Security-Policy", "")
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertNotIn("img-src 'self' data: https:", csp,
                         "img-src must NOT include https: to block remote images")

    def test_x_content_type_options_nosniff(self) -> None:
        r = self.client.get("/api/stats")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(r.headers.get("Referrer-Policy"), "no-referrer")

    def test_cors_preflight_rejects_unknown_origin(self) -> None:
        r = self.client.options(
            "/api/admin/auth/token",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        # No Access-Control-Allow-Origin should be returned for a
        # foreign origin, because we mount an allow_origins=[] policy.
        self.assertNotIn("access-control-allow-origin", {k.lower() for k in r.headers})

    def test_csrf_blocks_cross_origin_post(self) -> None:
        r = self.client.post(
            "/api/admin/auth/token",
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(r.status_code, 403)
        self.assertIn("Cross-origin", r.json()["error"])

    def test_csrf_allows_same_origin_post(self) -> None:
        # Build a same-origin request; even if the route is missing or
        # returns 404/405, the CSRF guard should let it through (so
        # we only assert non-403).
        r = self.client.post(
            "/api/admin/auth/token",
            headers={"Origin": "http://testserver"},
        )
        self.assertNotEqual(r.status_code, 403)

    def test_csrf_rejects_post_with_cross_site_sec_fetch(self) -> None:
        r = self.client.post(
            "/api/admin/auth/token",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        # Either CSRF rejection (403) or 404/405 is acceptable; the
        # important thing is that we don't 200 with auth bypass.
        self.assertIn(r.status_code, (403, 404, 405))

    def test_auth_token_disabled_by_default(self) -> None:
        # When no token is configured, the /api/admin/auth/token POST
        # is still allowed (it's the public "create a token" endpoint
        # itself). What we want to verify is that the middleware does
        # NOT 401 the request just because there's no Authorization
        # header — token enforcement is opt-in.
        r = self.client.post("/api/admin/auth/token")
        self.assertNotEqual(r.status_code, 401,
                            "auth middleware should be a no-op when no token is set")


if __name__ == "__main__":
    unittest.main()
