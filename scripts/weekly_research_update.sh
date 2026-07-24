#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${LOOP_MEMORY_REPO:-${SCRIPT_DIR:h}}
STATE_DIR=${LOOP_MEMORY_AUTOMATION_STATE_DIR:-$HOME/.loop_memory/automation}
LOG_DIR="$STATE_DIR/logs"
LOCK_DIR="$STATE_DIR/weekly-research.lock"
PRIVATE_ENV_FILE="$STATE_DIR/env"
RUN_DATE=$(date +%F)
BRANCH="automation/weekly-research-$RUN_DATE"
CODEX_BIN=${CODEX_BIN:-/Applications/ChatGPT.app/Contents/Resources/codex}

umask 077
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/weekly-research-$RUN_DATE.log") 2>&1

if [[ -f "$PRIVATE_ENV_FILE" ]]; then
  ENV_MODE=$(stat -f '%Lp' "$PRIVATE_ENV_FILE")
  if (( (8#$ENV_MODE & 8#077) != 0 )); then
    echo "$PRIVATE_ENV_FILE must not be readable by group or others." >&2
    exit 1
  fi
  set -a
  source "$PRIVATE_ENV_FILE"
  set +a
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Another weekly research run is active; exiting."
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

echo "[$(date -Iseconds)] Starting Loop Memory weekly research"
cd "$REPO_ROOT"

for command in git gh python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done
[[ -x "$CODEX_BIN" ]] || {
  echo "Codex executable not found: $CODEX_BIN" >&2
  exit 1
}

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Repository has local changes. Skipping to avoid mixing private or unfinished work."
  exit 0
fi
if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "Repository is not on main. Skipping safely."
  exit 0
fi

git fetch origin --prune
LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse origin/main)
if [[ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]]; then
  echo "Local main and origin/main differ. Resolve them before the next run."
  exit 1
fi

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "Branch $BRANCH already exists; refusing to overwrite it."
  exit 1
fi
git switch -c "$BRANCH"

PROMPT=$(cat <<'EOF'
You are running the weekly open-source research and improvement cycle for Loop Memory.

Research first:
1. Search GitHub releases, changelogs, issues, papers, engineering blogs, and other public web sources from roughly the last seven days.
2. Compare relevant agent-memory and long-term-memory projects, including Mem0, Letta, Zep/Graphiti, LangMem, OpenMemory, and newly discovered peers.
3. Prefer primary sources. Record source URLs, publication/update dates, licenses, notable changes, applicability, and explicit adopt/defer/reject decisions.
4. Never copy incompatible code. Reimplement only general ideas that fit this MIT project and add attribution when required.

Update the repository:
1. Create docs/research/YYYY-MM-DD.md with a concise evidence-based report, even if no code change is justified.
2. Implement only small, high-confidence improvements that materially benefit Loop Memory and fit its architecture. Avoid speculative rewrites and dependency growth.
3. Add or update focused tests and user documentation for every behavior change.
4. Run focused validation while iterating.

Safety rules:
- Do not read ~/.loop_memory/secrets.json, ~/.codex/auth.json, shell history, .env files, keychains, or any credential store.
- Do not include local transcripts, databases, logs, user paths, personal data, tokens, API keys, or credentials in repository files.
- Do not commit, push, open pull requests, merge, publish packages, or modify Git configuration.
- Do not weaken security checks, CI, secret scanning, or tests.
- Leave the working tree with only the intended research report and justified project changes.
EOF
)

"$CODEX_BIN" exec \
  --ephemeral \
  --cd "$REPO_ROOT" \
  --sandbox workspace-write \
  -c 'approval_policy="never"' \
  -c 'sandbox_workspace_write.network_access=true' \
  "$PROMPT"

if [[ -z "$(git status --porcelain)" ]]; then
  echo "Codex produced no repository changes."
  git switch main
  git branch -D "$BRANCH"
  exit 0
fi

python3 scripts/scan_secrets.py
python3 -m py_compile scripts/scan_secrets.py

if [[ -x .venv/bin/ruff ]]; then
  .venv/bin/ruff check loop_memory tests
else
  python3 -m ruff check loop_memory tests
fi
if [[ -x .venv/bin/pytest ]]; then
  .venv/bin/pytest -q
else
  python3 -m pytest -q
fi

git add -A
python3 scripts/scan_secrets.py --tracked-only
git diff --cached --check
git commit -m "chore(research): weekly ecosystem update $RUN_DATE"
git push --set-upstream origin "$BRANCH"

PR_URL=$(gh pr create \
  --base main \
  --head "$BRANCH" \
  --title "chore(research): weekly ecosystem update $RUN_DATE" \
  --body "Automated weekly ecosystem research and high-confidence improvements. Local secret scan, lint, and tests passed before push. CI must pass before automatic squash merge.")
echo "Created $PR_URL"

if gh pr checks "$PR_URL" --watch --fail-fast --interval 30; then
  gh pr merge "$PR_URL" --squash --delete-branch
  git switch main
  git pull --ff-only origin main
  echo "Merged and synchronized $PR_URL"
else
  echo "CI failed or was cancelled. The remote branch and PR remain for review." >&2
  exit 1
fi

echo "[$(date -Iseconds)] Weekly research completed"
