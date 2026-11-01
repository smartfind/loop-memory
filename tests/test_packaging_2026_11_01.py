"""Packaging regression tests.

Audit 2026-11-01 (loop-memory 0.4.19).

The 0.4.18 wheel shipped only ``loop_memory/py.typed`` and
``loop_memory/serve/static/index.html``.  Every CSS / JS / i18n /
image asset and the benchmark fixture were missing, so a PyPI install

* served a dashboard whose JavaScript and CSS 404'd, and
* crashed ``loop-memory bench`` with a ``FileNotFoundError`` on the
  packaged fixture.

The cause was a single ``pyproject.toml`` line::

    [tool.setuptools.package-data]
    loop_memory = ["py.typed", "serve/static/*.html"]

``serve/static/*.html`` matches ``index.html`` and nothing else -- the
patterns are not recursive, and ``loop_memory/bench/fixtures`` was not
declared at all.

These tests build the real wheel and sdist and assert that every
non-Python file in the package tree is actually inside them, so the
next asset someone adds cannot silently fall out of the distribution.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "loop_memory"

#: The fixture the benchmark pins.  Keep in sync with
#: ``tests/test_bench_2026_11_01.py``.
BENCH_FIXTURE = "loop_memory/bench/fixtures/default.json"
BENCH_FIXTURE_SHA256 = (
    "417c4b5ff86a5f18d8bf1c5a540e6f9e7fcc022e5b360d3d91bb146610cddd67"
)


def _expected_data_files() -> set[str]:
    """Every non-Python file in the package tree, wheel-relative."""
    out: set[str] = set()
    for path in PKG_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        if rel.suffix == ".py" or "__pycache__" in rel.parts:
            continue
        out.add(rel.as_posix())
    return out


#: The files setuptools needs to build the distribution.  The test
#: copies exactly these into a scratch directory so the build sees a
#: pristine tree.
BUILD_INPUTS = ("pyproject.toml", "README.md", "LICENSE")


@pytest.fixture(scope="module")
def clean_source(tmp_path_factory):
    """A pristine copy of the build inputs.

    Building in the working tree is not faithful: setuptools caches the
    file list in ``loop_memory.egg-info/SOURCES.txt`` and copies the
    package into ``build/lib`` before packaging *that* copy.  A stale
    cache from an earlier build would ship files the current
    ``pyproject.toml`` no longer declares, and the test would pass for
    the wrong reason.  A fresh copy is what CI sees.
    """
    dest = tmp_path_factory.mktemp("src") / "loop-memory"
    dest.mkdir()
    for name in BUILD_INPUTS:
        shutil.copy2(REPO_ROOT / name, dest / name)
    shutil.copytree(
        PKG_ROOT,
        dest / "loop_memory",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return dest


@pytest.fixture(scope="module")
def built_wheel(clean_source, tmp_path_factory):
    """Build the real wheel with setuptools' PEP 517 backend."""
    from setuptools import build_meta

    outdir = tmp_path_factory.mktemp("dist")
    cwd = os.getcwd()
    try:
        os.chdir(clean_source)
        name = build_meta.build_wheel(str(outdir))
    finally:
        os.chdir(cwd)
    return outdir / name


@pytest.fixture(scope="module")
def built_sdist(clean_source, tmp_path_factory):
    """Build the real sdist with setuptools' PEP 517 backend."""
    from setuptools import build_meta

    outdir = tmp_path_factory.mktemp("sdist")
    cwd = os.getcwd()
    try:
        os.chdir(clean_source)
        name = build_meta.build_sdist(str(outdir))
    finally:
        os.chdir(cwd)
    return outdir / name


def _wheel_data_files(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as zf:
        return {
            name
            for name in zf.namelist()
            if not name.endswith(".py") and ".dist-info/" not in name
        }


# --------------------------------------------------------------------------
# The regression itself
# --------------------------------------------------------------------------


def test_wheel_ships_every_non_python_file(built_wheel):
    """Every asset in the package tree must reach the wheel.

    This is the invariant that 0.4.18 broke: a data file existed in
    the repository but was not declared as package data, so it never
    reached the distribution.
    """
    expected = _expected_data_files()
    shipped = _wheel_data_files(built_wheel)
    missing = sorted(expected - shipped)
    assert not missing, (
        "these files exist in the package tree but are not packaged "
        f"(declared in pyproject.toml [tool.setuptools.package-data]): {missing}"
    )


def test_wheel_ships_the_bench_fixture(built_wheel):
    """``loop-memory bench`` needs the fixture on disk."""
    assert BENCH_FIXTURE in _wheel_data_files(built_wheel)


def test_wheel_ships_the_serve_static_assets(built_wheel):
    """The dashboard needs its CSS, JS, i18n and images."""
    shipped = _wheel_data_files(built_wheel)
    required = [
        "loop_memory/serve/static/index.html",
        "loop_memory/serve/static/css/app.css",
        "loop_memory/serve/static/css/layout.css",
        "loop_memory/serve/static/js/main.js",
        "loop_memory/serve/static/js/store.js",
        "loop_memory/serve/static/js/lib/vue.esm-browser.prod.js",
        "loop_memory/serve/static/js/components/Dashboard.js",
        "loop_memory/serve/static/js/composables/useI18n.js",
        "loop_memory/serve/static/i18n/en.json",
        "loop_memory/serve/static/i18n/zh.json",
        "loop_memory/serve/static/img/logo.svg",
    ]
    missing = [name for name in required if name not in shipped]
    assert not missing, f"dashboard assets missing from the wheel: {missing}"


def test_wheel_fixture_bytes_are_unchanged(built_wheel):
    """The shipped fixture must be the same bytes the tests pin."""
    with zipfile.ZipFile(built_wheel) as zf:
        raw = zf.read(BENCH_FIXTURE)
    assert hashlib.sha256(raw).hexdigest() == BENCH_FIXTURE_SHA256


def test_sdist_ships_the_data_files(built_sdist):
    """The sdist is what a source install is built from."""
    with tarfile.open(built_sdist) as tf:
        names = tf.getnames()
    prefix = names[0].split("/")[0]
    for rel in (BENCH_FIXTURE, "loop_memory/serve/static/i18n/en.json"):
        assert f"{prefix}/{rel}" in names, f"{rel} missing from the sdist"


def _parse_package_data(text: str) -> dict[str, list[str]]:
    """Parse the ``[tool.setuptools.package-data]`` table.

    ``tomllib`` is stdlib only from Python 3.11 and this project
    supports 3.10, so the table is read directly.  It is a flat
    mapping of package name to a list of glob strings -- small enough
    that a TOML dependency would cost more than it saves, and the CI
    matrix runs 3.10 / 3.11 / 3.12.
    """
    out: dict[str, list[str]] = {}
    in_table = False
    key: str | None = None
    buf = ""

    def flush() -> None:
        nonlocal key, buf
        if key is not None:
            out[key] = re.findall(r'"([^"]*)"', buf)
            key = None
            buf = ""

    for line in text.splitlines():
        stripped = line.strip()
        if key is not None:
            buf += " " + stripped
            if buf.count("[") <= buf.count("]"):
                flush()
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            in_table = stripped == "[tool.setuptools.package-data]"
            continue
        if not in_table:
            continue
        name, _, value = stripped.partition("=")
        key = name.strip().strip('"')
        buf = value.strip()
        if buf.count("[") <= buf.count("]"):
            flush()
    flush()
    return out


def test_pyproject_declares_the_data_packages():
    """The declaration itself -- so the fix is readable, not implicit."""
    package_data = _parse_package_data(
        (REPO_ROOT / "pyproject.toml").read_text("utf-8")
    )
    assert "py.typed" in package_data["loop_memory"]
    assert "loop_memory.bench" in package_data
    assert "loop_memory.serve.static" in package_data


def test_the_declaration_check_runs_without_tomllib():
    """Python 3.10 has no ``tomllib`` -- the check must not need one.

    The 3.10 CI job failed on exactly this: the first version of the
    test imported ``tomllib``, which only exists from 3.11 onwards,
    while ``pyproject.toml`` declares ``requires-python = ">=3.10"``.
    """
    code = textwrap.dedent(
        """
        import importlib.util
        import pathlib
        import sys

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "tomllib":
                    raise ImportError("tomllib is Python 3.11+")
                return None

        sys.meta_path.insert(0, Blocker())
        sys.modules.pop("tomllib", None)

        root = pathlib.Path({root!r})
        spec = importlib.util.spec_from_file_location(
            "lm_packaging_probe", root / "tests" / "test_packaging_2026_11_01.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = module._parse_package_data(
            (root / "pyproject.toml").read_text("utf-8")
        )
        assert "loop_memory.bench" in data, data
        assert "loop_memory.serve.static" in data, data
        print("ok")
        """
    ).format(root=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# --------------------------------------------------------------------------
# End-to-end: run the bench out of an unpacked wheel
# --------------------------------------------------------------------------


def test_bench_runs_from_an_unpacked_wheel(built_wheel, tmp_path):
    """Import the wheel as an installed package and run the benchmark.

    The repository copy is an editable install, so it would shadow an
    unpacked wheel on ``sys.path``.  ``-S`` skips site-packages (and
    therefore the editable ``.pth`` finder) and ``PYTHONPATH`` puts the
    unpacked wheel first, which reproduces what a PyPI install sees.
    """
    site = tmp_path / "site"
    with zipfile.ZipFile(built_wheel) as zf:
        zf.extractall(site)

    code = (
        "from loop_memory.bench.runner import default_fixture_path, load_fixture\n"
        "p = default_fixture_path()\n"
        "assert p.is_file(), f'fixture not found: {p}'\n"
        "fixture, digest = load_fixture()\n"
        "assert digest == '" + BENCH_FIXTURE_SHA256 + "', digest\n"
        "print('ok')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site)
    env.pop("PYTHONHOME", None)
    proc = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "the packaged benchmark fixture does not resolve from an "
        f"installed layout:\n{proc.stderr}"
    )
    assert "ok" in proc.stdout


def test_serve_static_dir_has_its_assets_on_disk(built_wheel, tmp_path):
    """Unpacking the wheel must yield a usable static directory.

    ``loop_memory/serve/routes/system.py`` reads
    ``static/i18n/en.json`` and ``static/i18n/zh.json`` at request
    time and mounts ``/static`` from the same directory, so a missing
    asset is a runtime error, not a cosmetic one.
    """
    site = tmp_path / "site"
    with zipfile.ZipFile(built_wheel) as zf:
        zf.extractall(site)
    static = site / "loop_memory" / "serve" / "static"
    assert (static / "index.html").is_file()
    for rel in (
        "i18n/en.json",
        "i18n/zh.json",
        "css/app.css",
        "js/main.js",
        "js/lib/vue.esm-browser.prod.js",
    ):
        assert (static / rel).is_file(), f"missing static asset: {rel}"
