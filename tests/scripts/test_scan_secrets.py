"""Tests for ``scripts/scan_secrets.py``.

Mirrors the script's regex catalogue so the pre-push gate can never
silently drift away from the runtime redactor in
``loop_memory/privacy/redact.py``. Two patterns were added in the
2026-08-01 weekly-research cycle: JSON Web Tokens and
``Authorization: Bearer ...`` headers. They are pinned here.

The fixtures are built at test time via ``_make_fixture`` so the
literal secret-shaped strings never appear on their own line in this
file (which would otherwise trip the pre-push gate on this very
file). Each fixture is a single line containing the secret pattern
we want to match, plus enough surrounding text for the regex to
fire.
"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "scan_secrets.py"
)


def _load_module() -> object:
    """Import ``scripts/scan_secrets.py`` as a Python module.

    The script is a top-level tool, not part of the package, so we use
    ``importlib`` instead of going through ``loop_memory`` /
    ``scripts.__init__`` (which does not exist).
    """
    spec = importlib.util.spec_from_file_location(
        "scan_secrets_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


# Per-pattern alphabet. AWS access keys are uppercase + digits;
# JWTs are base64url (uppercase, lowercase, digits, ``-``, ``_``);
# everything else accepts a broad character class.
_FIXTURE_ALPHABET: dict[str, str] = {
    "AWS access key": "ABCDEFGHIJKLMNOP0123456789",
    "Google API key": "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "JWT": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",
}

# Per-pattern minimum length. The scanner's regexes anchor the match
# length to a minimum (e.g. AWS: exactly 16 alphanumerics after the
# AKIA prefix). Building a tail that's ``min_len`` long and then
# immediately terminated by a non-alphanumeric guarantees the trailing
# ``\b`` lands correctly.
_FIXTURE_MIN_LEN: dict[str, int] = {
    "private key": 0,            # the BEGIN line is enough
    "OpenAI-style API key": 24,
    "GitHub token": 26,
    "Anthropic API key": 24,
    "Google API key": 32,
    "AWS access key": 16,        # exact length
    "Slack token": 20,
    "JWT": 64,                   # header + payload + signature
    "Bearer token": 24,
}


def _make_fixture(prefix: str, body: str) -> str:
    """Build a one-line fixture that looks like ``prefix<suffix>``.

    The ``prefix`` is the literal secret kind header (e.g. ``sk-``)
    and ``body`` is the long opaque tail. The function joins them
    with a non-letter so the pre-push gate's own regex won't see the
    literal pattern on its own line in this file's source. The tail
    is terminated by a non-alphanumeric char (the space before
    ``-``) so patterns that anchor on a trailing ``\b`` fire
    cleanly.
    """
    alphabet = _FIXTURE_ALPHABET.get(body, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    min_len = _FIXTURE_MIN_LEN.get(body, 24)
    tail = "".join(alphabet[i % len(alphabet)] for i in range(min_len))
    return f"# fixture: {prefix}{tail} - {body}\n"


# Per-pattern fixture prefix. Each one is the literal header the
# regex looks for. The tail is built by ``_make_fixture``.
_FIXTURE_PREFIX: dict[str, str] = {
    # The private-key regex only inspects the BEGIN line; the
    # placeholder check runs on the matched value itself, so we plant
    # ``EXAMPLE`` inside the prefix to make the fixture self-filtering.
    "private key": "-----BEGIN EXAMPLE ABC PRIVATE KEY-----\nMIIB\n-----END EXAMPLE ABC PRIVATE KEY-----\n",
    "OpenAI-style API key": "sk-",
    "GitHub token": "ghp_",
    "Anthropic API key": "sk-ant-",
    "Google API key": "AIza",
    "AWS access key": "AKIA",
    "Slack token": "xoxb-",
    "JWT": (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    ),
    "Bearer token": "Bearer ",
}


class PatternCatalogueTests(unittest.TestCase):
    """Each ``PATTERNS`` entry matches a synthetic fixture.

    Pinned so future drift in the regex catalog breaks the test
    loudly without leaning on the placeholder filter (which would
    drop real-looking secrets and hide the regression).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_every_pattern_matches_a_fixture(self) -> None:
        for label, pattern in self.mod.PATTERNS.items():
            with self.subTest(label=label):
                prefix = _FIXTURE_PREFIX.get(label)
                self.assertIsNotNone(
                    prefix, f"no fixture registered for pattern: {label}"
                )
                fixture = _make_fixture(prefix, label)
                self.assertIsNotNone(
                    pattern.search(fixture),
                    f"pattern {label!r} did not match its fixture:\n{fixture!r}",
                )

    def test_alignment_with_runtime_redactor(self) -> None:
        # Sanity: the public-facing patterns the runtime redactor
        # ships in ``loop_memory/privacy/redact.py`` are present in
        # the pre-push scanner too. This is the lock-step invariant
        # the new comment on ``PATTERNS`` promises.
        labels = set(self.mod.PATTERNS)
        for required in (
            "private key",
            "OpenAI-style API key",
            "GitHub token",
            "Anthropic API key",
            "Google API key",
            "AWS access key",
            "Slack token",
            "JWT",
            "Bearer token",
        ):
            self.assertIn(required, labels, f"missing pattern: {required}")


class PlaceholderFilterTests(unittest.TestCase):
    """The ``PLACEHOLDER_MARKERS`` catalogue is the only thing that
    keeps test fixtures and documentation examples from tripping the
    pre-push gate. Pinned to a small, well-known set so a renaming
    surfaces here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_known_markers_are_recognised(self) -> None:
        for marker in (
            "example",
            "placeholder",
            "replace_me",
            "your_",
            "your-",
            "dummy",
            "fake",
            "test",
            "xxxx",
        ):
            with self.subTest(marker=marker):
                self.assertTrue(
                    self.mod.looks_like_placeholder(marker),
                    f"marker {marker!r} is not recognised",
                )

    def test_clean_value_is_not_a_placeholder(self) -> None:
        # A plausible-but-unmarked string must NOT be filtered.
        self.assertFalse(self.mod.looks_like_placeholder("abcdefghijk"))


class EndToEndScanFileTests(unittest.TestCase):
    """The integrated path (``scan_file``) runs the regex catalogue
    AND the placeholder filter together. We use only placeholder /
    benign fixtures here so the test file itself stays clean."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()
        cls.tmp_dir = Path(cls.mod.__file__).parent

    def _write(self, name: str, body: str) -> Path:
        target = self.tmp_dir / f"_tmp_scan_{name}.txt"
        target.write_text(textwrap.dedent(body), encoding="utf-8")
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        return target

    def _scan(self, name: str, body: str) -> list[tuple[str, int]]:
        target = self._write(name, body)
        return self.mod.scan_file(target, target.parent)

    def test_placeholder_jwt_is_filtered(self) -> None:
        # A JWT with ``xxxx`` markers (base64url allows ``x``) must
        # be filtered by the placeholder check.
        body = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        )
        findings = self._scan("jwt_ph", body)
        labels = [label for label, _ in findings]
        self.assertNotIn("JWT", labels)

    def test_benign_source_passes(self) -> None:
        body = textwrap.dedent(
            """
            # example settings file
            DEBUG = True
            NAME = "loop-memory"
            NOTES = \"\"\"we use a real key from a vault and it
            never lands in the repository\"\"\"
            """
        )
        findings = self._scan("clean", body)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
