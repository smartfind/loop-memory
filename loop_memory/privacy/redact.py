"""Pattern-based secret redaction.

The redaction layer runs **before** anything that touches long-term
storage (memory upserts, distilled wiki body, ask/inject output, export
markdown) so a leaked key never leaves the ingest path.

Design choices:

* Deterministic — same input always yields the same output. The
  patterns are ordered by length so the longest match wins on
  overlaps (an AWS key is preferred over a generic 32-char
  blob in the same span).
* Idempotent — re-running redact on already-redacted text is a
  no-op (placeholders are recognised and skipped).
* Surgeries only the secret — the surrounding context is kept so
  the LLM still sees *what* was being talked about
  (``set OPENAI_API_KEY=sk-…`` becomes
  ``set OPENAI_API_KEY=[REDACTED:openai_key]``).
* Cheap — pure regex, no model call, microseconds on a 4 KB
  transcript fragment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ----- Pattern catalogue ------------------------------------------------
# Each entry is (kind, compiled regex). Order matters: longer / more
# specific patterns must come first so a ``sk-…`` token isn't matched
# as a generic 32-char blob. ``redact_text`` walks the list top-down.

REDACT_KINDS: tuple[str, ...] = (
    "private_key_block",        # -----BEGIN ... PRIVATE KEY-----
    "openai_key",               # sk-<not ant/proj/svc>...
    "openai_project_key",       # sk-proj-...
    "openai_service_key",       # sk-svc-...
    "anthropic_key",            # sk-ant-...
    "gemini_key",               # AIza...  (Google AI Studio)
    "github_pat",               # ghp_ / gho_ / ghs_ / ghu_ / ghr_
    "slack_token",              # xoxb- / xoxp- / xoxa- / xoxs-
    "aws_access_key",           # AKIA / ASIA
    "jwt",                      # eyJ...eyJ...{sig}
    "bearer_token",             # ``Bearer xxx`` in HTTP headers
    "generic_high_entropy",     # fallback: 40+ char opaque blob in
                                # an env-style assignment (``KEY=xxx``,
                                # ``"api_key": "xxx"``)
)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Markers in long-term constants used to bypass subsequent patterns
    # (so re-running on already-redacted text doesn't re-match them).
    ("__redacted_placeholder__", re.compile(r"\[REDACTED:[a-z_]+\]")),

    # Code-fence-style private keys (capture the whole BEGIN..END block).
    ("private_key_block",
     re.compile(
         r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP |)PRIVATE KEY-----"
         r"[\s\S]*?-----END (?:RSA |DSA |EC |OPENSSH |PGP |)PRIVATE KEY-----",
         re.MULTILINE,
     )),

    # Order matters: more specific patterns MUST come first so the
    # generic OpenAI matcher doesn't scoop up an Anthropic key.
    ("openai_project_key",
     re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai_service_key",
     re.compile(r"\bsk-svc-[A-Za-z0-9_\-]{20,}")),
    ("anthropic_key",
     re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    # OpenAI user keys: ``sk-...`` where the suffix is NOT a known
    # sub-brand. Excludes ``sk-ant-``, ``sk-proj-``, ``sk-svc-`` so
    # those don't fall through to this generic bucket.
    ("openai_key",
     re.compile(r"\bsk-(?!ant-|proj-|svc-)[A-Za-z0-9_\-]{20,}")),
    ("gemini_key",
     re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b")),
    ("github_pat",
     re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{25,}\b")),
    ("slack_token",
     re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key",
     re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_api_key",
     re.compile(r"\bAIzaSy[A-Za-z0-9_-]{33}\b")),

    # JWT: three dot-separated base64url segments, the first being
    # always ``eyJ...``.
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),

    # Bearer token (used in HTTP headers and tool outputs).
    ("bearer_token",
     re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.~+\/]{20,}=*")),
]

# Generic high-entropy fallbacks — only when context suggests a key
# (env-style assignment or quoted JSON value).
_GENERIC_ENV = re.compile(
    r"""(?ix)
    (?: ^ | [\s,;{(] )
    (?: (?:[A-Z][A-Z0-9_]{2,})            # ALL_CAPS_KEY_NAME
        | ["']?(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)["']?
    )
    \s*[:=]\s*
    ["']?
    ([A-Za-z0-9_\-\.~+\/]{40,}=*)      # long opaque blob
    ["']?
    """,
)


@dataclass
class RedactionSummary:
    """Counts of redactions, by kind. Returned by :func:`redact_text`
    so callers (telemetry, UI preview) can show "redacted 3 API
    keys" without re-running the regexes.
    """
    counts: dict[str, int] = field(default_factory=dict)
    total_chars: int = 0        # how many characters were replaced

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def add(self, kind: str, n_chars: int) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.total_chars += n_chars

    def merge(self, other: RedactionSummary) -> None:
        for k, v in other.counts.items():
            self.counts[k] = self.counts.get(k, 0) + v
        self.total_chars += other.total_chars


def _placeholder(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def redact_text(text: str, *, summary: RedactionSummary | None = None) -> str:
    """Return ``text`` with every recognised secret replaced by a
    short placeholder.

    The transformation is identity if no pattern matches. A summary
    can be supplied to accumulate counts across many calls.
    """
    if not text:
        return text
    out = text
    for kind, pat in _PATTERNS:
        if kind == "__redacted_placeholder__":
            continue
        # Replace, tracking match length for the summary.
        def _sub(m: re.Match[str], redaction_kind: str = kind) -> str:
            n = len(m.group(0))
            if summary is not None:
                summary.add(redaction_kind, n)
            return _placeholder(redaction_kind)
        out = pat.sub(_sub, out)

    # Generic fallback: only when an env-style assignment is found.
    def _gen_sub(m: re.Match[str]) -> str:
        opaq = m.group(1)
        if not opaq:
            return m.group(0)
        n = len(opaq)
        if summary is not None:
            summary.add("generic_high_entropy", n)
        # Preserve the leading key name + separator.
        # The capture is the opaque blob; rebuild surrounding context.
        # m.group(0) has full match like 'API_KEY="abcdef..."'.
        prefix = m.group(0).rsplit(opaq, 1)[0]
        return prefix + _placeholder("generic_high_entropy")
    out = _GENERIC_ENV.sub(_gen_sub, out)

    return out


def redact_batch(texts: Iterable[str]) -> list[str]:
    """Apply :func:`redact_text` to a batch of strings.

    A shared summary is returned separately if needed
    (``RedactionSummary`` is on ``summary``; this helper only
    returns the redacted texts).
    """
    summary = RedactionSummary()
    return [redact_text(t, summary=summary) for t in texts]
