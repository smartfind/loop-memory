#!/usr/bin/env python3
"""Fail safely when repository files appear to contain private credentials."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


MAX_FILE_BYTES = 2 * 1024 * 1024
# Pattern catalogue. Mirrors loop_memory/privacy/redact.py so the
# pre-push gate can never be silently less strict than the runtime
# redactor. Keep these in lock-step when adding a new kind.
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "OpenAI-style API key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    # JSON Web Tokens: three base64url segments separated by dots, the
    # first always eyJ... (base64 for {"). Very common in
    # mock API fixtures — the placeholder check below filters the
    # obvious test cases.
    "JWT": re.compile(
        r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    ),
    # Authorization: Bearer <opaque> headers / tool outputs. Matches
    # the redaction layer so a stray token in a transcript cannot slip
    # past the local gate.
    "Bearer token": re.compile(
        r"(?i)\bBearer\s+[A-Za-z0-9_\-.,~+/]{20,}=*"
    ),
}
ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)\b"
    r"\s*[:=]\s*['\"]([^'\"\s]{16,})['\"]"
)
PLACEHOLDER_MARKERS = (
    "example",
    "placeholder",
    "replace_me",
    "replace-with",
    "your_",
    "your-",
    "dummy",
    "fake",
    "test",
    "xxxx",
    "${",
    "{{",
)


def repository_files(root: Path, tracked_only: bool) -> list[Path]:
    command = ["git", "ls-files", "-z"]
    if not tracked_only:
        command = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    result = subprocess.run(command, cwd=root, check=True, capture_output=True)
    return [root / item.decode() for item in result.stdout.split(b"\0") if item]


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def scan_file(path: Path, root: Path) -> list[tuple[str, int]]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return []
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\0" in raw:
        return []

    findings: list[tuple[str, int]] = []
    text = raw.decode("utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in PATTERNS.items():
            match = pattern.search(line)
            if match and not looks_like_placeholder(match.group(0)):
                findings.append((label, line_number))
        assignment = ASSIGNMENT_PATTERN.search(line)
        if assignment and not looks_like_placeholder(assignment.group(1)):
            findings.append(("credential-like assignment", line_number))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="scan only files already tracked by Git",
    )
    args = parser.parse_args()

    root_result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
    )
    root = Path(root_result.stdout.strip())
    findings: list[tuple[Path, str, int]] = []
    for path in repository_files(root, args.tracked_only):
        for label, line_number in scan_file(path, root):
            findings.append((path.relative_to(root), label, line_number))

    if findings:
        print("Secret scan failed. Potential private data found:", file=sys.stderr)
        for path, label, line_number in findings:
            print(f"  {path}:{line_number}: {label}", file=sys.stderr)
        print("Values are intentionally redacted. Remove or rotate them before pushing.", file=sys.stderr)
        return 1

    scope = "tracked files" if args.tracked_only else "tracked and untracked repository files"
    print(f"Secret scan passed ({scope}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
