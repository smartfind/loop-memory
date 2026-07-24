"""User-controlled ``<private>...</private>`` span handling.

Users can mark ranges they want kept out of the long-term store
entirely (``This is my actual birthday: <private>1990-01-01</private>``)
or just out of distillation / recall. The span delimiters are
preserved so the agent still sees the marker itself, but the
content inside is replaced by ``[PRIVATE:redacted]``.

Pattern notes:

* Case-insensitive (``<PRIVATE>...</PRIVATE>`` too).
* Non-greedy so multiple spans per turn all get caught.
* Multi-line: spans may span newlines.
"""

from __future__ import annotations

import re

_PRIVATE_RE = re.compile(
    r"<private>([\s\S]*?)</private>",
    re.IGNORECASE,
)


def strip_private_spans(text: str, *, replacement: str | None = None) -> str:
    """Replace every ``<private>...</private>`` body with a marker.

    Default replacement is ``"[PRIVATE:redacted]"``. Pass a different
    ``replacement`` if your pipeline needs something else (e.g. an
    empty string when the entire span is also being dropped).
    """
    if not text:
        return text
    rep = replacement if replacement is not None else "[PRIVATE:redacted]"
    return _PRIVATE_RE.sub(rep, text)


def has_private_blocks(text: str) -> bool:
    """``True`` iff ``text`` contains at least one private span
    (after stripping content). Useful as a cheap pre-filter so
    callers don't run distillation on text that will be empty.
    """
    if not text:
        return False
    return _PRIVATE_RE.search(text) is not None
