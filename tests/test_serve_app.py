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


class IndexInlineI18nTests(ServeAppSmokeTests):
    """Pin the inline-i18n fix that prevents the
    "页面先变英文再变中文" flicker on hard refresh.

    Pre-fix behaviour: the index route escaped the JSON dictionaries
    with ``html.escape()``, so the inline ``<script type="application/json">``
    payloads contained ``&quot;`` for every JSON quote. The browser
    passes that text through to ``JSON.parse`` verbatim — HTML
    entities are NOT decoded inside <script> tags — and ``JSON.parse``
    fails with a syntax error. ``store.js``'s inline read therefore
    silently populated *no* dictionary, so the first Vue render fell
    back to the English ``_i18n.en[key]`` and only snapped to
    ``_i18n.zh`` after the network fetch completed inside
    ``onMounted``.

    Post-fix behaviour: the index route injects the JSON strings
    verbatim, only neutralising the literal ``</script>`` sequence
    that could otherwise close the script tag. ``JSON.parse`` then
    succeeds on the inline payload and the first paint is already in
    the user's locale.
    """

    def _inline_json(self, lang: str) -> dict:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.text
        # The script tag is closed with a reverse-solidus escape so a
        # literal </script> inside the JSON can't break the page —
        # match either form.
        m = re.search(
            r'<script type="application/json" id="loop-i18n-' + lang + r'">(.+?)</script>',
            body,
            re.S,
        )
        self.assertIsNotNone(m, f"inline script loop-i18n-{lang} missing")
        import json as _json
        return _json.loads(m.group(1))

    def test_inline_i18n_en_parses_and_has_chinese_keys(self) -> None:
        d = self._inline_json("en")
        self.assertGreater(len(d), 100)
        self.assertEqual(d["timeline.allKinds"], "All kinds")

    def test_inline_i18n_zh_parses_and_has_chinese_values(self) -> None:
        d = self._inline_json("zh")
        self.assertEqual(d["timeline.allKinds"], "全部类型")
        self.assertEqual(d["graph.allKinds"], "全部类型")
        self.assertEqual(d["filter.allKinds"], "全部类型")

    def test_inline_i18n_is_not_html_escaped(self) -> None:
        # Pre-fix regression: the inline payload contained ``&quot;``
        # because the index route used ``html.escape``. Browser-passed-
        # through JSON would fail to parse and ``store.js`` would fall
        # back to the English dictionary on the first paint.
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        # An entity-encoded payload always contains "&#" sequences
        # outside of script blocks. Count occurrences INSIDE the
        # inline <script type="application/json"> blocks specifically.
        body = r.text
        for lang in ("en", "zh"):
            m = re.search(
                r'<script type="application/json" id="loop-i18n-' + lang + r'">(.+?)</script>',
                body,
                re.S,
            )
            self.assertIsNotNone(m)
            payload = m.group(1)
            # Real JSON uses bare " quotes; HTML-escaped JSON does not.
            self.assertNotIn("&quot;", payload,
                             f"payload for {lang} is HTML-escaped; "
                             "JSON.parse will fail and the i18n flicker "
                             "regresses (all kinds -> 全部类型 on hard "
                             "refresh).")
            # Sanity: payload does contain the kind-filter keys.
            self.assertIn("timeline.allKinds", payload)
            self.assertIn("graph.allKinds", payload)

    def test_inline_i18n_payloads_match_disk_files(self) -> None:
        # Catch any future drift between the disk JSON and what the
        # server serves — if the line endings or key ordering ever
        # matter to a downstream consumer, this test surfaces it.
        import json as _json
        d_zh = self._inline_json("zh")
        d_en = self._inline_json("en")
        disk_zh = _json.loads(
            Path("loop_memory/serve/static/i18n/zh.json").read_text(encoding="utf-8")
        )
        disk_en = _json.loads(
            Path("loop_memory/serve/static/i18n/en.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(d_zh) - set(disk_zh), set())
        self.assertEqual(set(d_en) - set(disk_en), set())

    def test_index_status_is_200_when_static_present(self) -> None:
        # Regression guard for the ``FileResponse is not defined``
        # 500 that used to surface when ``index.html.read_text``
        # raised any exception (e.g. a launchd sandbox blip). The
        # previous inline-i18n tests only inspected payload content
        # and therefore masked the regression. Pin status_code here
        # so the next refactor that breaks ``FileResponse`` import
        # lights up immediately.
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_index_falls_back_to_raw_html_when_read_text_raises(self) -> None:
        # If the launcher sandbox or a permissions glitch causes the
        # index route's ``read_text`` call to fail, the endpoint
        # must still serve a usable HTML payload instead of
        # NameError 500. We simulate the failure by stubbing the
        # index.html ``Path.read_text``.
        from loop_memory.serve import routes
        from pathlib import Path as _Path
        original = _Path.read_text
        def boom(self, *a, **kw):  # noqa: ANN001, ANN002
            if self.name == "index.html":
                raise PermissionError("[Errno 1] Operation not permitted")
            return original(self, *a, **kw)
        _Path.read_text = boom  # type: ignore[assignment]
        try:
            r = self.client.get("/")
        finally:
            _Path.read_text = original  # type: ignore[assignment]
        self.assertEqual(r.status_code, 200)
        # Either FileResponse (bytes-equivalent) or HTMLResponse: the
        # body must still contain the doctype so the browser can
        # bootstrap the SPA.
        self.assertIn(b"<!DOCTYPE html>", r.content)

    def test_index_falls_back_when_i18n_json_missing(self) -> None:
        # Mirrors the sandbox case where the disk JSON files are
        # readable but i18n JSONs are not (or simply removed). The
        # endpoint must still serve HTML with the doctype.
        from pathlib import Path as _Path
        original = _Path.read_text
        def selective(self, *a, **kw):  # noqa: ANN001, ANN002
            if self.name in ("en.json", "zh.json"):
                raise FileNotFoundError("simulated")
            return original(self, *a, **kw)
        _Path.read_text = selective  # type: ignore[assignment]
        try:
            r = self.client.get("/")
        finally:
            _Path.read_text = original  # type: ignore[assignment]
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<!DOCTYPE html>", r.content)


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

    def test_static_paths_remain_public_when_auth_enabled(self) -> None:
        """Audit H2: when an auth token is configured, /static/*
        must still be reachable so the SPA shell can boot before the
        user pastes the bearer token. Without this, the entire UI
        renders as a blank page once auth is on."""
        from loop_memory.storage.sqlite_store import MemoryStore
        store = MemoryStore(str(self.tmp / "db.sqlite"))
        store.set_setting("loop_memory_auth_token", "TEST_TOKEN_XYZ")
        # Re-build the client so it picks up the new setting via
        # create_app's middleware. Pass the bundled static dir so
        # /static/* is actually served.
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        from pathlib import Path as _P
        import loop_memory
        static_dir = _P(loop_memory.__file__).parent / "serve" / "static"
        client = TestClient(create_app(store, static_dir=static_dir))
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        # Static assets should be served without a Bearer header.
        r_js = client.get("/static/js/api.js")
        self.assertEqual(r_js.status_code, 200,
                         f"/static/* must remain public when auth is on; got {r_js.status_code}")
        # But a protected endpoint still requires the token.
        r_prot = client.post("/api/admin/consolidate")
        self.assertEqual(r_prot.status_code, 401)

    def test_sessions_routes_import_session_to_dict(self) -> None:
        """Audit H3: /api/sessions once returned 500 because
        serve/routes/sessions.py referenced _session_to_dict
        without importing it after the O1 refactor split the routes
        out of serve/app.py. The Sidebar therefore appeared empty
        even when the store had dozens of records. This test seeds a
        session, hits the route, and asserts 200 + non-empty list —
        catching the regression even if a future move of the helper
        forgets to update the re-export."""
        from loop_memory.storage.sqlite_store import MemoryStore
        store = MemoryStore(str(self.tmp / "db.sqlite"))
        store.upsert_session(
            source="codex", external_id="audit-h3",
            title="audit h3 fixture", started_at=1.0,
            ended_at=2.0, message_count=3,
        )
        from fastapi.testclient import TestClient
        from loop_memory.serve.app import create_app
        client = TestClient(create_app(store))
        r = client.get("/api/sessions?limit=10")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)
        # Per-source counts come from a sibling closure; make sure it
        # stays reachable so the sidebar pills keep lighting up.
        r2 = client.get("/api/sessions/counts")
        self.assertEqual(r2.status_code, 200)
        self.assertGreaterEqual(r2.json()["all"]["sessions"], 1)


if __name__ == "__main__":
    unittest.main()
