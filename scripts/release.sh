#!/usr/bin/env bash
# Local PyPI release helper.
#
# Default path uses GitHub Actions Trusted Publishing (pypa/gh-action-pypi-publish)
# so no API token ever has to live on disk. The workflow is .github/workflows/publish.yml.
#
# Local-only fallback for the very first upload (PyPI requires you to log in
# once to register the Trusted Publisher):
#
#   export TWINE_USERNAME=__token__
#   export TWINE_PASSWORD=pypi-<your token>
#   ./scripts/release.sh --upload
#
# Without --upload this script only verifies: clean working tree, version in
## pyproject matches the requested tag, sdist+wheel build cleanly, twine check
## passes, and git tag does not already exist locally.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

usage() {
  cat >&2 <<EOF
usage: $(basename "$0") [--upload] [--tag <vX.Y.Z>] [--skip-tests]

Options:
  --upload           actually upload to PyPI (default: dry-run / verify only)
  --tag <vX.Y.Z>     tag to publish (default: derived from pyproject.toml version)
  --skip-tests       skip running pytest+ruff (use only if you just ran them)
EOF
  exit 2
}

UPLOAD=0
TAG=""
SKIP_TESTS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --upload) UPLOAD=1; shift ;;
    --tag) TAG="$2"; shift 2 ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

VERSION=$(grep -E '^version\s*=\s*".+"' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/') || true
if [[ -z "$VERSION" ]]; then
  echo "could not parse version from pyproject.toml" >&2
  exit 2
fi
if [[ -z "$TAG" ]]; then
  TAG="v$VERSION"
fi

if [[ "$TAG" != "v$VERSION" ]]; then
  echo "tag $TAG does not match pyproject version v$VERSION" >&2
  exit 2
fi

echo "== loop-memory $TAG =="
echo "    pyproject version : $VERSION"
echo "    tag               : $TAG"
echo "    upload            : $([[ $UPLOAD -eq 1 ]] && echo yes || echo "no (dry-run)")"

# 1) Working tree must be clean.
if ! git diff --quiet; then
  echo "working tree has uncommitted changes:"; git status --short; exit 2
fi
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "working tree has untracked files:"; git status --short; exit 2
fi

# 2) Branch must be main (or whatever the project's protected branch is).
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" != "main" ]]; then
  echo "refusing to release from branch '$BRANCH' (must be main)"; exit 2
fi

# 3) Tests + lint.
if [[ $SKIP_TESTS -eq 0 ]]; then
  if [[ -x .venv/bin/ruff ]]; then
    .venv/bin/ruff check .
  elif command -v ruff >/dev/null 2>&1; then
    ruff check .
  else
    echo "ruff not found; run --skip-tests only after running it locally" >&2
    exit 2
  fi
  if [[ -x .venv/bin/pytest ]]; then
    .venv/bin/pytest -q
  elif command -v pytest >/dev/null 2>&1; then
    pytest -q
  else
    echo "pytest not found; run --skip-tests only after running it locally" >&2
    exit 2
  fi
fi

# 4) Tag must not already exist.
if git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null; then
  echo "tag $TAG already exists locally"; exit 2
fi

# 5) Build.
test -x .venv/bin/python && PY=.venv/bin/python || PY=python3
"$PY" -m pip install --quiet build twine
"$PY" -m build --sdist --wheel

# 6) Twine metadata check (must pass before upload).
"$PY" -m twine check dist/*

# 7) Optional upload.
if [[ $UPLOAD -eq 1 ]]; then
  "$PY" -m twine upload dist/*
else
  echo
  echo "Dry run only. To publish:"
  echo "    1. git tag $TAG"
  echo "    2. git push origin $TAG"
  echo "    3. GitHub Actions will build, run twine check, publish via Trusted Publishing,"
  echo "       and draft a GitHub release."
  echo
  echo "Or to upload locally with a token (first release only):"
  echo "    TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-...  ./scripts/release.sh --upload"
fi
