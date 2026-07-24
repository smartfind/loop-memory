"""Privacy utilities — secret redaction & private-tag handling.

Goals:

* Never let a recognised secret (API key, JWT, SSH private key, …)
  reach the long-term store. Redact in-place so the surrounding
  context is preserved (e.g. "I set OPENAI_API_KEY=sk-…1234"
  becomes "I set OPENAI_API_KEY=[REDACTED:openai_key]") and the
  LLM still knows roughly what was being talked about.
* Honour the user-controlled ``<private>...</private>`` tags to
  mark regions that should be skipped entirely.
* Be deterministic so re-running distillation on the same text
  produces the same redactions (no drift on round-trips).
"""

from .redact import redact_text, redact_batch, RedactionSummary, REDACT_KINDS
from .private import strip_private_spans, has_private_blocks

__all__ = [
    "redact_text", "redact_batch", "RedactionSummary", "REDACT_KINDS",
    "strip_private_spans", "has_private_blocks",
]
