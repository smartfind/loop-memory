# Weekly ecosystem research automation

Loop Memory can run a local, weekly research cycle that surveys comparable open-source projects
and public technical sources, records evidence, implements only high-confidence improvements, and
pushes the result through a protected GitHub pull request.

The automation runs locally rather than placing an LLM API key in GitHub Actions. It uses the
existing Codex and GitHub CLI logins, stores logs under `~/.loop_memory/automation/` with restrictive
permissions, and never writes credentials to the repository.

## Default schedule

Install the schedule on macOS:

```bash
scripts/install_weekly_research_launchd.sh
```

The default is Monday at 03:00 in the Mac's local timezone. To choose another time, pass the
launchd weekday (`1` is Monday; `0` or `7` is Sunday), hour, and minute:

```bash
scripts/install_weekly_research_launchd.sh 6 3 30
```

Inspect or remove the job:

```bash
launchctl print gui/$UID/com.loopmemory.weekly-research
launchctl bootout gui/$UID/com.loopmemory.weekly-research
rm ~/Library/LaunchAgents/com.loopmemory.weekly-research.plist
```

## Safety and delivery gates

Each run:

1. Stops without changing anything unless the repository is clean, on `main`, and exactly synced
   with `origin/main`.
2. Creates an isolated `automation/weekly-research-YYYY-MM-DD` branch.
3. Asks Codex to research primary sources from the previous week and write
   `docs/research/YYYY-MM-DD.md` with links, dates, licenses, and adopt/defer/reject decisions.
4. Allows only focused, tested improvements and forbids Codex from reading credential stores,
   committing, pushing, or weakening safety checks.
5. Scans tracked and untracked files for common API keys, tokens, private keys, and credential-like
   assignments without printing detected values.
6. Runs Ruff and the full Pytest suite, scans again after staging, and checks the staged diff.
7. Pushes a remote branch, opens a pull request, waits for GitHub CI, and squash-merges only when all
   required checks pass. Failed checks leave the pull request open for review.

GitHub also runs `.github/workflows/secret-scan.yml` for every pull request and push to `main`.

## Operations

Logs are written to `~/.loop_memory/automation/logs/weekly-research-YYYY-MM-DD.log`. A failed run is
safe to rerun after resolving the reported problem. If a same-day automation branch already exists,
inspect or remove it before rerunning; the script never overwrites an existing branch.

Run the cycle manually with:

```bash
scripts/weekly_research_update.sh
```

The machine must be awake and logged in at the scheduled time. macOS normally runs a missed
`StartCalendarInterval` job after waking, but it cannot run while powered off.

### Optional private environment

`launchd` does not inherit shell proxy variables. Put machine-specific network settings in
`~/.loop_memory/automation/env`, then restrict the file to the current user:

```bash
cat > ~/.loop_memory/automation/env <<'EOF'
HTTPS_PROXY=http://127.0.0.1:PORT
HTTP_PROXY=http://127.0.0.1:PORT
EOF
chmod 600 ~/.loop_memory/automation/env
```

The runner refuses to load this file if group or other users can read it. Keep secrets out of the
file when possible; prefer the operating system keychain and existing Codex or `gh` authentication.
