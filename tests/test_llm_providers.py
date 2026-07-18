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
        self.assertLessEqual(cfg["behaviour"]["max_output_tokens"], 4096)
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
