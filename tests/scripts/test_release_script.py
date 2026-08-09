"""Regression tests for the PyPI release helper ``scripts/release.sh``.

The script enforces five guards before letting you upload to PyPI:

    1. Working tree must be clean (no diffs, no untracked files).
    2. Current branch must be ``main``.
    3. ``ruff`` and ``pytest`` must be available (or ``--skip-tests``).
    4. The tag must not already exist locally.
    5. ``pyproject.toml`` version must match the requested tag.

Pinned here so any future refactor can never silently drop a guard.
The fixtures stand up an isolated git repository that mirrors the
real project's layout (script under ``scripts/`` + pyproject at the
repo root) so the script's guards can fire as if they were running
against the real tree, without ever touching the real PyPI.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT_SRC = REPO / "scripts" / "release.sh"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run a git command inside ``cwd`` and return stdout."""
    return subprocess.run(
        ("git",) + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    ).stdout.strip()


@pytest.fixture()
def release_workspace(tmp_path: Path) -> Path:
    """A self-contained git repo whose pyproject / script mimic the real project.

    Skips automatically if bash / git / ruff / pytest are unavailable.
    """
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash or git missing in this environment")
    if shutil.which("ruff") is None and not (Path(".venv") / "bin" / "ruff").exists():
        pytest.skip("ruff not available; cannot exercise success path")
    if shutil.which("pytest") is None and not (Path(".venv") / "bin" / "pytest").exists():
        pytest.skip("pytest not available; cannot exercise success path")

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "scripts").mkdir()
    (ws / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [build-system]
            requires = ["setuptools>=68"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "loop-memory"
            version = "0.4.0"
            """
        )
    )
    shutil.copy(SCRIPT_SRC, ws / "scripts" / "release.sh")
    os.chmod(ws / "scripts" / "release.sh", 0o755)

    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "ci@example.com")
    _git(ws, "config", "user.name", "ci")
    _git(ws, "config", "commit.gpgsign", "false")
    _git(ws, "add", "pyproject.toml", "scripts/release.sh")
    _git(ws, "commit", "-q", "-m", "init")
    return ws


def _run(ws: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke ``scripts/release.sh`` inside the isolated workspace."""
    return subprocess.run(
        ("bash", "scripts/release.sh", "--skip-tests") + args,
        cwd=ws,
        capture_output=True,
        text=True,
    )


def test_release_source_keeps_all_guards() -> None:
    """Belt-and-braces: even if the runtime fixtures skip, the script on
    disk must still contain each guard clause."""
    src = SCRIPT_SRC.read_text()
    assert "git diff --quiet" in src, "must guard against unstaged changes"
    assert "git status --porcelain --untracked-files=all" in src, (
        "must guard against untracked files"
    )
    assert "must be main" in src, "must require branch == main"
    assert "tag $TAG already exists" in src, "must guard against duplicate tag"
    assert "does not match pyproject version" in src, (
        "must match tag to pyproject version"
    )


def test_release_refuses_uncommitted_changes(release_workspace: Path) -> None:
    """Tracked edit to any committed file must be rejected before any build.

    Uses a fresh file (not pyproject.toml) so the version parser still
    succeeds and the script reaches its working-tree guards.
    """
    (release_workspace / "notes.txt").write_text("scratch\n")
    _git(release_workspace, "add", "notes.txt")
    (release_workspace / "notes.txt").write_text("mutated\n")
    proc = _run(release_workspace)
    assert proc.returncode != 0
    assert "uncommitted" in proc.stdout + proc.stderr


def test_release_refuses_untracked_files(release_workspace: Path) -> None:
    """Untracked file must trip the porcelain guard."""
    (release_workspace / "scratch.txt").write_text("scratch\n")
    proc = _run(release_workspace)
    assert proc.returncode != 0
    assert "untracked" in proc.stdout + proc.stderr


def test_release_refuses_non_main_branch(release_workspace: Path) -> None:
    """Switching to a feature branch must hard-fail before the build runs."""
    _git(release_workspace, "checkout", "-q", "-b", "feature/x")
    proc = _run(release_workspace)
    assert proc.returncode != 0
    assert "main" in proc.stdout + proc.stderr


def test_release_refuses_existing_tag(release_workspace: Path) -> None:
    """A tag that already exists must block the release."""
    _git(release_workspace, "tag", "v0.4.0")
    proc = _run(release_workspace)
    assert proc.returncode != 0
    assert "already exists" in proc.stdout + proc.stderr


def test_release_refuses_tag_version_mismatch(release_workspace: Path) -> None:
    """A --tag that disagrees with pyproject.toml must abort up front."""
    proc = _run(release_workspace, "--tag", "v9.9.9")
    assert proc.returncode != 0
    assert "does not match" in proc.stdout + proc.stderr


def test_release_parses_version_and_reaches_build(release_workspace: Path) -> None:
    """All guards passed ⇒ script prints the parsed version + derived tag
    before any build work. This proves the version parser and tag
    derivation work end-to-end without depending on the actual build
    (the test environment may lack a usable system pip / venv).
    """
    proc = _run(release_workspace)
    assert "pyproject version : 0.4.0" in proc.stdout
    assert "tag               : v0.4.0" in proc.stdout
    # Proves no earlier guard tripped — build failure after this point is
    # unrelated to release safety.
    assert "must be main" not in proc.stdout + proc.stderr
    assert "already exists" not in proc.stdout + proc.stderr
    assert "does not match" not in proc.stdout + proc.stderr
