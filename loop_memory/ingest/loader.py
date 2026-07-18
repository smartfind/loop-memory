"""Conversation ingest — turn local assistant transcripts into memories.

Each loader normalises a different tool's transcript shape into a
common ``IngestedSession`` model so the storage layer can ingest them
uniformly.

Currently supported:

- **Codex CLI**: ``~/.codex/sessions/**/*.jsonl`` (the event-stream
  format actually written by Codex Desktop / CLI today).
- **Claude Code**: ``~/.claude/history.jsonl`` plus per-project JSONL.
- **Hermes / generic**: any JSONL of ``{"role":...,"content":...}``.

Adding a new tool: subclass ``BaseLoader`` and put the file-glob +
parser in ``_parse``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IngestedTurn:
    role: str   # "user" | "assistant" | "system"
    text: str
    created_at: float | None = None


@dataclass
class IngestedSession:
    source: str                # "codex" | "claude" | "hermes" | "generic"
    external_id: str
    title: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    turns: list[IngestedTurn] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return len(self.turns)


# ---------- helpers ---------------------------------------------------------

_TEXT_BLOCK_RE = re.compile(r"[\s\n]+")


def _norm(text: str) -> str:
    if not text:
        return ""
    return _TEXT_BLOCK_RE.sub(" ", text).strip()


def _to_float(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        # seconds-or-ms heuristic
        if ts > 1e12:
            return float(ts) / 1000.0
        return float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            try:
                return float(ts)
            except Exception:
                return None
    return None


def _read_text(record) -> str:
    """Extract best-effort text from arbitrary Codex/Claude content shapes."""
    if record is None:
        return ""
    if isinstance(record, str):
        return _norm(record)
    if isinstance(record, list):
        out: list[str] = []
        for part in record:
            if isinstance(part, dict):
                out.append(_read_text(part.get("text")))
            elif isinstance(part, str):
                out.append(part)
        return _norm(" ".join(s for s in out if s))
    if isinstance(record, dict):
        for key in ("text", "content", "input_text", "output_text", "message"):
            if key in record:
                t = _read_text(record[key])
                if t:
                    return t
    return ""


# ---------- base ------------------------------------------------------------

class BaseLoader(ABC):
    source: str = "generic"

    @abstractmethod
    def discover(self, root: Path) -> Iterable[Path]:
        ...

    def discover_all(self, root: Path) -> list[Path]:
        return list(self.discover(root))

    def load_one(self, path: Path) -> IngestedSession | None:
        try:
            return self._parse(path)
        except Exception:
            return None

    @abstractmethod
    def _parse(self, path: Path) -> IngestedSession | None:
        ...


# ---------- codex ------------------------------------------------------------

class CodexLoader(BaseLoader):
    """Codex CLI events: a JSONL stream of session events.

    We accept both the live shape (each line has ``type``/``payload``)
    and the older flat ``[{"role":...,"content":...}]`` shape.
    Each file maps to a single ``IngestedSession``.
    """

    source = "codex"

    def discover(self, root: Path) -> Iterable[Path]:
        root = Path(root).expanduser()
        if not root.exists():
            return []
        yield from sorted(root.glob("**/*.jsonl"))
        yield from sorted(root.glob("**/*.json"))

    def _parse(self, path: Path) -> IngestedSession | None:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            return None
        # First try: live event-stream JSONL.
        if "\n" in text and text.startswith("{"):
            return self._parse_event_stream(path)
        # Otherwise: flat JSON array / dict with messages.
        return self._parse_flat(path)

    # -- parsers ------------------------------------------------------------

    def _parse_event_stream(self, path: Path) -> IngestedSession | None:
        turns: list[IngestedTurn] = []
        started: float | None = None
        ended: float | None = None
        external_id: str | None = None
        title: str | None = None
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            ts = _to_float(rec.get("timestamp"))

            if external_id is None:
                payload = rec.get("payload") or {}
                if isinstance(payload, dict):
                    external_id = (
                        payload.get("id")
                        or payload.get("session_id")
                        or rec.get("session_id")
                    )

            rtype = (rec.get("type") or "").lower()
            payload = rec.get("payload") or {}

            # session_meta: capture the id but emit no turn
            if rtype in {"session_meta", "session.started"}:
                continue

            # User message: payload = { role, content: [{type, text}] }
            if rtype in {"user", "user_message", "human"} or (
                rtype == "message" and (payload.get("role") or "").lower() == "user"
            ):
                content = payload.get("content") if isinstance(payload, dict) else None
                text = _read_text(content)
                if text:
                    turns.append(IngestedTurn("user", text, ts))
                    if title is None:
                        title = text[:80]
                    if started is None or (ts and ts < started):
                        started = ts
                    if ended is None or (ts and ts > ended):
                        ended = ts
                continue

            # Assistant message: payload = { role, content: [{type, text}] }
            if rtype in {"assistant", "assistant_message", "ai"} or (
                rtype == "message" and (payload.get("role") or "").lower() == "assistant"
            ):
                content = payload.get("content") if isinstance(payload, dict) else None
                text = _read_text(content)
                if text:
                    turns.append(IngestedTurn("assistant", text, ts))
                    if ended is None or (ts and ts > ended):
                        ended = ts
                continue

            # Codex also emits "response_item" events with content arrays
            if rtype in {"response_item", "item"}:
                role = (payload.get("role") or "").lower() if isinstance(payload, dict) else ""
                if role in {"user", "assistant"}:
                    content = payload.get("content") if isinstance(payload, dict) else None
                    text = _read_text(content)
                    if text:
                        turns.append(IngestedTurn(role, text, ts))
                        if role == "user" and title is None:
                            title = text[:80]
                        if started is None or (ts and ts < started):
                            started = ts
                        if ended is None or (ts and ts > ended):
                            ended = ts

        if not turns:
            return None
        return IngestedSession(
            source=self.source,
            external_id=external_id or str(path.stem),
            title=title,
            started_at=started or time.time(),
            ended_at=ended,
            turns=turns,
        )

    def _parse_flat(self, path: Path) -> IngestedSession | None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            messages = data.get("messages") or data.get("conversation") or []
        elif isinstance(data, list):
            messages = data
        else:
            return None
        turns: list[IngestedTurn] = []
        started: float | None = None
        ended: float | None = None
        title: str | None = None
        for m in messages:
            role = (m.get("role") or "").strip().lower()
            text = _norm(str(m.get("content") or m.get("text") or m.get("message") or ""))
            ts = _to_float(m.get("ts") or m.get("timestamp") or m.get("created_at"))
            if not text:
                continue
            turns.append(IngestedTurn(role, text, ts))
            if started is None or (ts and ts < started):
                started = ts
            if ended is None or (ts and ts > ended):
                ended = ts
            if title is None and role == "user":
                title = text[:80]
        if not turns:
            return None
        return IngestedSession(
            source=self.source,
            external_id=str(path.stem),
            title=title,
            started_at=started or time.time(),
            ended_at=ended,
            turns=turns,
        )


# ---------- claude -----------------------------------------------------------

class ClaudeLoader(BaseLoader):
    """Claude Code.

    We accept two shapes that exist on real machines:

    1. ``~/.claude/history.jsonl`` — every line is a single user prompt::

         {"display": "...", "timestamp": <ms>, "project": "...", "sessionId": "..."}

    2. Per-session JSONL in any ``**/*.jsonl`` with the Claude Code
       ``{"type":"user|assistant", "message": {"role":..., "content":...}}`` shape.
    """

    source = "claude"

    def discover(self, root: Path) -> Iterable[Path]:
        root = Path(root).expanduser()
        if not root.exists():
            return []
        yield from sorted(root.glob("**/sessions/*.jsonl"))
        yield from sorted(root.glob("history.jsonl"))
        yield from sorted(root.glob("**/*.jsonl"))

    def _parse(self, path: Path) -> IngestedSession | None:
        if path.name == "history.jsonl":
            return self._parse_history(path)
        return self._parse_session(path)

    def _parse_history(self, path: Path) -> IngestedSession | None:
        """Group ``history.jsonl`` lines by ``sessionId`` → one session per file.

        ``history.jsonl`` only contains user prompts, but it's the most
        consistently-written log on a Claude Code install, so we make
        the most of it: each session is the list of user prompts in
        a single file.
        """
        groups: dict[str, list[IngestedTurn]] = {}
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sid = rec.get("sessionId") or rec.get("session_id") or "default"
            display = _norm(str(rec.get("display") or rec.get("content") or ""))
            ts = _to_float(rec.get("timestamp"))
            if not display:
                continue
            groups.setdefault(sid, []).append(IngestedTurn("user", display, ts))

        if not groups:
            return None

        # Largest session wins → the file usually represents one user.
        sid, turns = max(groups.items(), key=lambda kv: len(kv[1]))
        ts_list = [t.created_at for t in turns if t.created_at]
        started = min(ts_list) if ts_list else time.time()
        ended = max(ts_list) if ts_list else None

        return IngestedSession(
            source=self.source,
            external_id=str(path.stem) + "::" + sid[:8],
            title=turns[0].text[:80] if turns else None,
            started_at=started,
            ended_at=ended,
            turns=turns,
        )

    def _parse_session(self, path: Path) -> IngestedSession | None:
        turns: list[IngestedTurn] = []
        started: float | None = None
        ended: float | None = None
        title: str | None = None
        external_id: str | None = None
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if not isinstance(msg, dict):
                continue
            role = (msg.get("role") or rec.get("type") or "").strip().lower()
            content = msg.get("content")
            text = _read_text(content)
            if not text:
                continue
            ts = _to_float(rec.get("ts") or rec.get("timestamp"))
            turns.append(IngestedTurn(role, text, ts))
            if started is None or (ts and ts < started):
                started = ts
            if ended is None or (ts and ts > ended):
                ended = ts
            if title is None and role == "user":
                title = text[:80]
            if external_id is None:
                external_id = rec.get("sessionId") or rec.get("session_id")
        if not turns:
            return None
        return IngestedSession(
            source=self.source,
            external_id=external_id or str(path.stem) + "::" + uuid.uuid4().hex[:6],
            title=title,
            started_at=started or time.time(),
            ended_at=ended,
            turns=turns,
        )


# ---------- hermes -----------------------------------------------------------

class HermesLoader(BaseLoader):
    """Generic JSONL of ``{"role":..., "content":...}`` per line."""

    source = "hermes"

    def discover(self, root: Path) -> Iterable[Path]:
        root = Path(root).expanduser()
        if not root.exists():
            return []
        yield from sorted(root.glob("**/*.jsonl"))

    def _parse(self, path: Path) -> IngestedSession | None:
        turns: list[IngestedTurn] = []
        started: float | None = None
        ended: float | None = None
        title: str | None = None
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            role = (rec.get("role") or "").strip().lower()
            text = _norm(str(rec.get("content") or rec.get("text") or ""))
            if not text:
                continue
            ts = _to_float(rec.get("ts") or rec.get("timestamp") or rec.get("created_at"))
            turns.append(IngestedTurn(role, text, ts))
            if started is None or (ts and ts < started):
                started = ts
            if ended is None or (ts and ts > ended):
                ended = ts
            if title is None and role == "user":
                title = text[:80]
        if not turns:
            return None
        return IngestedSession(
            source=self.source,
            external_id=str(path),
            title=title,
            started_at=started or time.time(),
            ended_at=ended,
            turns=turns,
        )




# ---------- openclaw --------------------------------------------------------

class OpenClawLoader(BaseLoader):
    """OpenClaw session transcripts (clawx client + clawx OpenClaw agent).

    Real-world file format (clawx main agent):

        L1  {"type": "session", "id": "<uuid>", "timestamp": "<iso>",
             "cwd": "...", "version": "3"}
        L2  {"type": "model_change", ...}
        L3  {"type": "thinking_level_change", ...}
        L4  {"type": "custom", "customType": "model-snapshot", ...}
        L5  {"type": "message", "id": "...", "timestamp": "<iso>",
             "message": {"role": "user"|"assistant"|"toolResult",
                         "content": [{"type": "text", "text": "..."} |
                                     {"type": "thinking", "thinking": "..."} |
                                     {"type": "toolCall", "name": "exec",
                                      "arguments": {"command": "..."}} |
                                     {"type": "toolResult", "content": [...]}],
                         "timestamp": <epoch_ms>}}
        ...

    Companion files (skipped to avoid double counting):
        <id>.trajectory.jsonl  - runtime trace events
        <id>.trajectory-path.json
        <id>.checkpoint.<uuid>.jsonl - mid-run snapshots

    We also still accept the legacy flat shape:

        {"role": "...", "content": "...", "ts": <epoch or ISO8601>}

    so existing JSONL exports keep working.

    Files live under any of:
        ~/.openclaw/agents/main/sessions/*.jsonl   (clawx real path)
        ~/.openclaw/sessions/*.jsonl
        ~/.openclaw/workspace/memory/*.md          (clawx daily logs)

    `discover()` walks ``**/*.jsonl`` + ``**/*.json`` + ``**/*.md``.
    """

    source = "openclaw"

    # Companion suffixes to skip — they describe the same session but
    # at a finer granularity than we need (trajectory/checkpoint).
    _COMPANION_SUFFIXES = (
        ".trajectory.jsonl",
        ".trajectory-path.json",
        ".checkpoint.",
    )

    # Subdirectories under ~/.openclaw that contain real transcripts.
    # Anything else (extensions/, plugins/, npm/, node_modules/, ...) is
    # skipped so we don't try to ingest 1000s of config / vendor files.
    _WHITELIST_DIRS = (
        "agents/main/sessions",
        "sessions",
        "workspace/memory",
        "memory",
    )

    def discover(self, root: Path) -> Iterable[Path]:
        root = Path(root).expanduser()
        if not root.exists():
            return []
        seen: set = set()
        # 1) If the root itself matches a whitelist dir, scan it directly.
        rel = None
        try:
            rel = str(root.relative_to(Path.home() / ".openclaw"))
        except ValueError:
            pass
        if rel and any(rel == d or rel.startswith(d) for d in self._WHITELIST_DIRS):
            roots = [root]
        else:
            # 2) Otherwise pick all whitelist dirs that exist under root.
            roots = [root / d for d in self._WHITELIST_DIRS if (root / d).exists()]
            if not roots:
                # Fallback: shallow scan of root (one level only) so a
                # custom path like /tmp/foo/ still works.
                roots = [root]

        for base in roots:
            for pat in ("**/*.jsonl", "**/*.json", "**/*.md"):
                for p in sorted(base.glob(pat)):
                    # Skip companion files describing the same session
                    if any(s in p.name for s in self._COMPANION_SUFFIXES):
                        continue
                    if p.name.endswith(".trajectory.json"):
                        continue
                    # Skip session index / pointer metadata files
                    if p.name in ("sessions.json", "trajectory-path.json"):
                        continue
                    if p in seen:
                        continue
                    # Skip vendor / node_modules anywhere they appear
                    parts = set(p.parts)
                    if parts & {"node_modules", ".git", "dist", "build"}:
                        continue
                    seen.add(p)
                    yield p

    def _parse(self, path: Path) -> IngestedSession | None:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            return None
        # Markdown daily logs (clawx workspace/memory/*.md)
        if path.suffix.lower() == ".md":
            return self._parse_markdown(path, text)
        # Real clawx main-agent format
        if text.startswith("{") and ('"type": "session"' in text or '"type":"session"' in text):
            return self._parse_clawx_session(path, text)
        # Legacy shapes: single-JSON with messages, or flat JSONL of role/content
        return self._parse_jsonl(path)

    # --- clawx main-agent format -----------------------------------------

    def _parse_clawx_session(self, path: Path, text: str) -> IngestedSession | None:
        """Parse the actual clawx / OpenClaw main-agent JSONL where each
        line is an event with ``type=session|message|model_change|...``.
        We only care about ``type=message`` records; everything else
        (model change, custom snapshots, thinking-level changes) is
        context we don't surface as a turn."""
        import json
        session_id = path.stem
        cwd = None
        started: float | None = None
        ended: float | None = None
        title: str | None = None
        turns: list[IngestedTurn] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            rtype = rec.get("type")
            if rtype == "session":
                if rec.get("id"):
                    session_id = str(rec["id"])
                cwd = rec.get("cwd") or cwd
                ts = _to_float(rec.get("timestamp"))
                if ts and (started is None or ts < started):
                    started = ts
                continue
            if rtype != "message":
                continue
            msg = rec.get("message") or {}
            role = (msg.get("role") or "").strip().lower() or "assistant"
            ts_ms = msg.get("timestamp") or rec.get("timestamp")
            ts = _to_float(ts_ms)
            content = msg.get("content")
            text_part = self._extract_message_text(content)
            if not text_part:
                continue
            turns.append(IngestedTurn(role, text_part, ts))
            if started is None or (ts and ts < started):
                started = ts
            if ended is None or (ts and ts > ended):
                ended = ts
            if title is None and role == "user":
                title = text_part[:80]
        if not turns:
            return None
        if cwd and title:
            title = f"{title}  ·  {cwd}"
        elif cwd:
            title = cwd
        return IngestedSession(
            source=self.source,
            external_id=session_id,
            title=title,
            started_at=started or time.time(),
            ended_at=ended,
            turns=turns,
        )

    @staticmethod
    def _extract_message_text(content) -> str:
        """Pull the user-visible text out of a message.content which can
        be a string, a list of typed parts (text / thinking / toolCall /
        toolResult), or a dict."""
        if content is None:
            return ""
        if isinstance(content, str):
            return _norm(content)
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for p in content:
            if not isinstance(p, dict):
                parts.append(str(p))
                continue
            t = p.get("type")
            if t == "text":
                if p.get("text"):
                    parts.append(str(p["text"]))
            elif t == "thinking":
                # keep a brief marker so we don't lose the assistant's plan
                think = p.get("thinking") or ""
                if think:
                    parts.append("[thinking] " + str(think)[:400])
            elif t == "toolCall":
                name = p.get("name") or "tool"
                args = p.get("arguments") or {}
                if isinstance(args, dict) and "command" in args:
                    parts.append(f"[toolCall:{name}] {str(args['command'])[:200]}")
                else:
                    parts.append(f"[toolCall:{name}] {str(args)[:200]}")
            elif t == "toolResult":
                inner = p.get("content") or p.get("text") or ""
                if isinstance(inner, list):
                    inner = OpenClawLoader._extract_message_text(inner)
                if inner:
                    parts.append(f"[toolResult] {str(inner)[:300]}")
            else:
                if p.get("text"):
                    parts.append(str(p["text"]))
                elif p.get("content"):
                    parts.append(str(p["content"])[:300])
        return _norm("\n".join(parts))

    # --- Markdown daily logs ---------------------------------------------

    def _parse_markdown(self, path: Path, text: str) -> IngestedSession | None:
        """clawx writes daily memory journals under workspace/memory/*.md.
        These are not conversations but they ARE the user's distilled
        memory. We ingest each as a single 'reflection' turn so the
        consolidator can pick them up alongside normal sessions."""
        title = None
        for line in text.splitlines():
            if line.strip().startswith("# "):
                title = line.strip()[2:].strip()[:120]
                break
        if not title:
            title = path.stem
        # Heuristic timestamp from filename (2026-05-31.md) or mtime
        ts: float | None = None
        try:
            from datetime import datetime
            ts = datetime.strptime(path.stem[:10], "%Y-%m-%d").timestamp()
        except Exception:
            try:
                ts = path.stat().st_mtime
            except Exception:
                ts = time.time()
        turn = IngestedTurn("reflection", _norm(text), ts)
        return IngestedSession(
            source=self.source,
            external_id=path.stem,
            title=title,
            started_at=ts or time.time(),
            ended_at=ts,
            turns=[turn],
        )

    # --- legacy JSONL fallback -------------------------------------------

    def _parse_jsonl(self, path: Path) -> IngestedSession | None:
        turns: list[IngestedTurn] = []
        started: float | None = None
        ended: float | None = None
        title: str | None = None
        external_id = path.stem
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            role = (rec.get("role") or rec.get("speaker") or "").strip().lower()
            text = _norm(str(rec.get("content") or rec.get("text") or ""))
            if not text:
                continue
            ts = _to_float(rec.get("ts") or rec.get("timestamp") or rec.get("created_at"))
            turns.append(IngestedTurn(role, text, ts))
            if started is None or (ts and ts < started):
                started = ts
            if ended is None or (ts and ts > ended):
                ended = ts
            if title is None and role == "user":
                title = text[:80]
            if "session_id" in rec:
                external_id = str(rec["session_id"])
        if not turns:
            return None
        return IngestedSession(
            source=self.source,
            external_id=external_id,
            title=title,
            started_at=started or time.time(),
            ended_at=ended,
            turns=turns,
        )


# ---------- registry ---------------------------------------------------------


LOADERS = {
    "codex": CodexLoader,
    "claude": ClaudeLoader,
    "hermes": HermesLoader,
    "openclaw": OpenClawLoader,
}


def get_loader(source: str) -> BaseLoader:
    cls = LOADERS.get(source.lower())
    if cls is None:
        raise ValueError(f"unknown source: {source!r}; expected one of {sorted(LOADERS)}")
    return cls()


def default_paths() -> dict:
    """Best-guess roots per source — adjust to taste."""
    return {
        "codex": Path.home() / ".codex" / "sessions",
        "claude": Path.home() / ".claude",
        "hermes": Path.home() / ".hermes",
        # clawx OpenClaw stores real sessions under agents/main/sessions;
        # we still walk the broader ~/.openclaw so workspace/memory/*.md
        # daily journals and other agent dirs are picked up.
        "openclaw": Path.home() / ".openclaw",
    }
