from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from loop_memory.llm.base import ChatHistory, Message
from loop_memory.llm.providers import (
    PROVIDERS,
    AnthropicProvider,
    OllamaProvider,
    OpenAICompatProvider,
    RuleBasedProvider,
    build_provider,
    default_config,
    resolve_api_key,
    validate_config,
)


def _history() -> ChatHistory:
    return ChatHistory(
        system="be terse",
        messages=[
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ],
    )


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class OpenAICompatTests(unittest.TestCase):
    def test_complete_returns_message_content(self) -> None:
        body = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(body)):
            p = OpenAICompatProvider(model="m", api_key="k", base_url="https://x/v1")
            out = p.complete(_history(), max_tokens=10, temperature=0.1)
        self.assertEqual(out, "pong")

    def test_complete_handles_empty_choices(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(b"{}")):
            p = OpenAICompatProvider(api_key="k", base_url="https://x/v1")
            self.assertEqual(p.complete(_history()), "")

    def test_complete_raises_on_http_error(self) -> None:
        err = urllib.error.HTTPError(
            url="https://x/v1/chat/completions",
            code=401, msg="unauthorized", hdrs={}, fp=io.BytesIO(b"bad key"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            p = OpenAICompatProvider(api_key="k", base_url="https://x/v1")
            with self.assertRaises(RuntimeError):
                p.complete(_history())

    def test_api_key_env_fallback(self) -> None:
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "env-key"}, clear=False):
            p = OpenAICompatProvider(api_key=None, base_url="https://x/v1")
            self.assertEqual(p.api_key, "env-key")

    def test_base_url_env_fallback_OPENAI_BASE_URL(self) -> None:
        # Mirrors the official OpenAI SDK env-var name. When the
        # constructor base_url is empty and OPENAI_BASE_URL is set,
        # the provider should pick it up (and strip any trailing slash
        # so the URL builder doesn't emit a double slash).
        env = {"OPENAI_BASE_URL": "http://127.0.0.1:8080/v1/"}
        with mock.patch.dict("os.environ", env, clear=False):
            # Make sure the legacy name is not set so we know which
            # env var won the fallback.
            p = OpenAICompatProvider(api_key="k", base_url="")
            self.assertEqual(p.base_url, "http://127.0.0.1:8080/v1")

    def test_base_url_env_fallback_OPENAI_API_BASE(self) -> None:
        # Some proxies / forks still export OPENAI_API_BASE (the older
        # name). Kept as a secondary fallback so existing scripts that
        # already set the legacy variable keep working.
        env = {"OPENAI_API_BASE": "http://legacy-proxy:9000/v1"}
        with mock.patch.dict("os.environ", env, clear=False):
            p = OpenAICompatProvider(api_key="k", base_url="")
            self.assertEqual(p.base_url, "http://legacy-proxy:9000/v1")

    def test_explicit_base_url_wins_over_env(self) -> None:
        # Explicit constructor argument always wins — this preserves
        # the v0.x call sites that pass --base-url on the CLI and
        # keeps the existing tests green.
        env = {"OPENAI_BASE_URL": "http://from-env:1234/v1"}
        with mock.patch.dict("os.environ", env, clear=False):
            p = OpenAICompatProvider(api_key="k", base_url="https://explicit/v1")
            self.assertEqual(p.base_url, "https://explicit/v1")

    def test_default_base_url_when_no_env_or_arg(self) -> None:
        # Sanity: when neither the constructor argument nor any env var
        # is set, the provider still falls back to the public OpenAI
        # endpoint so the existing tests / examples keep working.
        with mock.patch.dict("os.environ", {}, clear=True):
            p = OpenAICompatProvider(api_key="k")
            self.assertEqual(p.base_url, "https://api.openai.com/v1")


class LLMEnvVarTests(unittest.TestCase):
    """Audit 2026-08-16: env-var plumb for LLM_TEMPERATURE / LLM_SEED.

    The OpenAI-compat / Anthropic / Ollama providers should honour
    ``LLM_TEMPERATURE`` and ``LLM_SEED`` as a global override so a
    user can pin deterministic distillation without touching the
    behaviour config (mirrors ``topoteretes/cognee`` v1.5.0 PR
    #4504). Explicit kwargs still win so existing call sites are
    unchanged.
    """

    @staticmethod
    def _capture_openai_body(env: dict[str, str], **kwargs) -> dict:
        body = json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["headers"] = dict(req.headers)
            return _FakeResp(body)

        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            p = OpenAICompatProvider(api_key="k", base_url="https://x/v1")
            p.complete(_history(), **kwargs)
        return captured.get("body", {})

    def test_llm_temperature_env_overrides_kwarg(self) -> None:
        body = self._capture_openai_body(
            {"LLM_TEMPERATURE": "0.05"}, temperature=0.9,
        )
        self.assertEqual(body["temperature"], 0.05)

    def test_llm_temperature_kwarg_wins_when_env_unset(self) -> None:
        body = self._capture_openai_body({}, temperature=0.42)
        self.assertEqual(body["temperature"], 0.42)

    def test_llm_temperature_falls_back_to_default(self) -> None:
        body = self._capture_openai_body({})
        self.assertEqual(body["temperature"], 0.3)

    def test_llm_temperature_ignores_invalid_env_value(self) -> None:
        body = self._capture_openai_body({"LLM_TEMPERATURE": "warm"})
        self.assertEqual(body["temperature"], 0.3)

    def test_llm_seed_env_adds_seed_field(self) -> None:
        body = self._capture_openai_body({"LLM_SEED": "17"})
        self.assertEqual(body["seed"], 17)

    def test_llm_seed_kwarg_wins_when_env_unset(self) -> None:
        body = self._capture_openai_body({}, seed=42)
        self.assertEqual(body["seed"], 42)

    def test_llm_seed_omitted_when_neither_set(self) -> None:
        body = self._capture_openai_body({})
        self.assertNotIn("seed", body)

    def test_llm_seed_ignores_invalid_env_value(self) -> None:
        body = self._capture_openai_body({"LLM_SEED": "not-a-number"})
        self.assertNotIn("seed", body)

    def test_anthropic_temperature_env_override(self) -> None:
        body = json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(body)

        with mock.patch.dict("os.environ",
                             {"LLM_TEMPERATURE": "0.01"}, clear=True), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            p = AnthropicProvider(api_key="k")
            p.complete(_history(), temperature=0.7)
        self.assertEqual(captured["body"]["temperature"], 0.01)
        # The seed override path also runs through the same helper.
        self.assertNotIn("seed", captured["body"])

    def test_anthropic_seed_env_sets_seed(self) -> None:
        body = json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(body)

        with mock.patch.dict("os.environ",
                             {"LLM_SEED": "2026"}, clear=True), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            p = AnthropicProvider(api_key="k")
            p.complete(_history())
        self.assertEqual(captured["body"]["seed"], 2026)

    def test_ollama_temperature_and_seed_through_options(self) -> None:
        body = json.dumps({"message": {"content": "ok"}}).encode()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(body)

        with mock.patch.dict("os.environ",
                             {"LLM_TEMPERATURE": "0.1",
                              "LLM_SEED": "9"}, clear=True), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            p = OllamaProvider(base_url="http://127.0.0.1:11434")
            p.complete(_history())
        self.assertEqual(captured["body"]["options"]["temperature"], 0.1)
        self.assertEqual(captured["body"]["options"]["seed"], 9)


class AnthropicProviderTests(unittest.TestCase):
    def test_joins_text_blocks(self) -> None:
        body = json.dumps({
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
                {"type": "tool_use", "text": "ignored"},
            ]
        }).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(body)):
            p = AnthropicProvider(api_key="k")
            self.assertEqual(p.complete(_history()), "hello " + chr(10) + "world")

    def test_folds_system_messages_into_system_prompt(self) -> None:
        captured = {}

        def fake_post(url, body, headers, timeout):
            captured["body"] = body
            return {"content": [{"type": "text", "text": "ok"}]}

        with mock.patch("loop_memory.llm.providers._http_post_json", side_effect=fake_post):
            p = AnthropicProvider(api_key="k")
            h = ChatHistory(
                system="base",
                messages=[
                    Message(role="system", content="extra rule"),
                    Message(role="user", content="hi"),
                ],
            )
            p.complete(h)
        self.assertIn("base", captured["body"]["system"])
        self.assertIn("extra rule", captured["body"]["system"])
        self.assertEqual(len(captured["body"]["messages"]), 1)


class OllamaProviderTests(unittest.TestCase):
    def test_reads_message_content(self) -> None:
        body = json.dumps({"message": {"content": "ok"}}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(body)):
            p = OllamaProvider(model="qwen2.5:7b", base_url="http://127.0.0.1:11434")
            self.assertEqual(p.complete(_history()), "ok")

    def test_missing_message_field_yields_empty_string(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp(b"{}")):
            p = OllamaProvider()
            self.assertEqual(p.complete(_history()), "")


class RuleBasedProviderTests(unittest.TestCase):
    def test_echoes_last_user(self) -> None:
        p = RuleBasedProvider()
        out = p.complete(_history())
        self.assertTrue(out.startswith("(rules) "))
        self.assertIn("hi", out)

    def test_truncates_long_user_input(self) -> None:
        p = RuleBasedProvider()
        h = ChatHistory(messages=[Message(role="user", content="x" * 500)])
        out = p.complete(h)
        self.assertLessEqual(len(out), len("(rules) ") + 120)


class BuildProviderTests(unittest.TestCase):
    def test_falls_back_when_key_missing(self) -> None:
        with mock.patch("loop_memory.llm.providers.resolve_api_key", return_value=None):
            p = build_provider({"provider": "openai", "model": "m"})
        self.assertIsInstance(p, RuleBasedProvider)

    def test_resolves_known_provider(self) -> None:
        with mock.patch("loop_memory.llm.providers.resolve_api_key", return_value="k"):
            p = build_provider({"provider": "openai", "model": "gpt-x"})
        self.assertIsInstance(p, OpenAICompatProvider)
        self.assertEqual(p.model, "gpt-x")

    def test_case_insensitive_provider_lookup(self) -> None:
        with mock.patch("loop_memory.llm.providers.resolve_api_key", return_value="k"):
            p = build_provider({"provider": "OpenAI", "model": "m"})
        self.assertIsInstance(p, OpenAICompatProvider)

    def test_unknown_provider_returns_rules(self) -> None:
        p = build_provider({"provider": "no-such-thing"})
        self.assertIsInstance(p, RuleBasedProvider)

    def test_anthropic_picked_when_provider_anthropic(self) -> None:
        with mock.patch("loop_memory.llm.providers.resolve_api_key", return_value="k"):
            p = build_provider({"provider": "anthropic"})
        self.assertIsInstance(p, AnthropicProvider)


class ResolveApiKeyTests(unittest.TestCase):
    def test_explicit_key_wins(self) -> None:
        self.assertEqual(resolve_api_key({"provider": "openai", "api_key": "x"}), "x")

    def test_secret_backend_lookup_when_account_set(self) -> None:
        with mock.patch("loop_memory.security.get_secret", return_value="from-kc") as gs:
            v = resolve_api_key({"provider": "openai", "api_key_account": "llm/openai/api_key"})
        self.assertEqual(v, "from-kc")
        gs.assert_called_once_with("llm/openai/api_key")

    def test_returns_none_when_no_source(self) -> None:
        with mock.patch("loop_memory.security.get_secret", return_value=None):
            self.assertIsNone(resolve_api_key({"provider": "openai"}))


class ValidateConfigTests(unittest.TestCase):
    def test_default_is_valid(self) -> None:
        cfg, warns = validate_config(default_config())
        self.assertEqual(warns, [])
        self.assertEqual(cfg["provider"], "echo")
        self.assertFalse(cfg["api_key_set"])

    def test_strips_api_key_field(self) -> None:
        cfg, _ = validate_config({"provider": "openai", "api_key": "leak"})
        self.assertNotIn("api_key", cfg)

    def test_clamps_temperature_and_tokens(self) -> None:
        cfg, _ = validate_config({
            "provider": "echo",
            "behaviour": {"temperature": 5.0, "max_output_tokens": 9999, "batch_size": -1},
        })
        self.assertLessEqual(cfg["behaviour"]["temperature"], 2.0)
        # Validator ceiling raised 4096 -> 8192 in v2 to match the
        # "completeness over compactness" distillation policy.
        self.assertLessEqual(cfg["behaviour"]["max_output_tokens"], 8192)
        self.assertEqual(cfg["behaviour"]["batch_size"], 1)

    def test_warns_on_unknown_provider(self) -> None:
        cfg, warns = validate_config({"provider": "Mystery"})
        self.assertIn(cfg["provider"], PROVIDERS)
        self.assertTrue(any("unknown provider" in w for w in warns))


class ProviderSpecTests(unittest.TestCase):
    def test_all_known_providers_resolve(self) -> None:
        for pid in ("MiniMax", "openai", "anthropic", "ollama", "echo"):
            self.assertIn(pid, PROVIDERS)
            self.assertTrue(PROVIDERS[pid].id)


if __name__ == "__main__":
    unittest.main()
