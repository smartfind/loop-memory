# Auto-capturing conversations into Loop Memory

Once you have a Loop Memory store running, you have three ways to
capture conversations automatically.

## 1. Filesystem watcher (recommended)

`loop-memory hook --source codex --watch ~/.codex/sessions` watches
the directory and ingests each new transcript once its size + mtime
have been stable for one poll interval. Run this as a long-lived
process — `tmux`, `screen`, `brew services`, or a systemd/launchd job.

```bash
# In a tmux session
loop-memory hook --source codex  --watch ~/.codex/sessions
loop-memory hook --source claude --watch ~/.claude/projects
loop-memory hook --source hermes --watch ~/.hermes
```

## 2. launchd (macOS)

The shipped launchd labels are `com.loopmemory.codex`, `com.loopmemory.claude`,
and `com.loopmemory.openclaw`. The legacy `com.loop-memory.*` label still works
if you've been running the project from an older install. New installs prefer
`com.loopmemory.<source>` so the same plist can be re-installed without
collisions.

Save the following at `~/Library/LaunchAgents/com.loopmemory.claude.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.loopmemory.claude</string>
  <key>ProgramArguments</key><array>
    <string>/Users/YOU/.local/bin/loop-memory</string>
    <string>hook</string>
    <string>--source</string><string>codex</string>
    <string>--watch</string><string>/Users/YOU/.codex/sessions</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>LOOP_MEMORY_DB</key><string>/Users/YOU/.loop_memory/loop_memory.db</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

Then:

```bash
launchctl load ~/Library/LaunchAgents/com.loopmemory.claude.plist
# or, if it was previously loaded under a different label:
launchctl bootout gui/$UID/com.loop-memory.watcher 2>/dev/null || true
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.loopmemory.claude.plist
```

### Operations

```bash
# status / pid
launchctl print gui/$UID/com.loopmemory.claude | rg 'state =|pid ='
# force a restart (picks up the latest code from the .venv)
launchctl kickstart -k gui/$UID/com.loopmemory.claude
# tail the loop-memory log
tail -f /tmp/loop_claude.log
```

If `state = waiting` and `pid = -`, the previous run exited; inspect
`/tmp/loop_claude.log` for the traceback. The most common cause after a code
pull is the launchd job still pointing at the system `python3`; reinstall the
plist so it uses the project's `.venv/bin/python`.

## 3. systemd (Linux)

Save at `~/.config/systemd/user/loop-memory.service`:

```ini
[Unit]
Description=Loop Memory watcher
After=default.target

[Service]
ExecStart=%h/.local/bin/loop-memory hook --source codex --watch %h/.codex/sessions
Environment=LOOP_MEMORY_DB=%h/.loop_memory/loop_memory.db
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user enable --now loop-memory.service
```

## 4. Scheduled consolidation

Memory gets stale even after you import it. Run the consolidator on
a timer to rescore, GC expired items, and merge duplicates.

```cron
# crontab -e — every hour
0 * * * *  loop-memory consolidate
```

## 5. One-shot historical import

For conversations that already exist on disk:

```bash
loop-memory ingest codex
loop-memory ingest claude
loop-memory ingest hermes
```


## 6. `loop-memory rules` — make the agent *use* the memory

`install-hooks` wires up the *capture* path (MCP server, SessionStart
inject). `rules` wires up the *recall-and-write-back* discipline on
the agent side: it appends a three-phase memory block
(`## At task start` / `## Mid-task` / `## Wrap-up`) into the agent's
own rule file, so every fresh session starts with the right muscle
memory.

```bash
# Print the generic block to stdout (no write)
loop-memory rules

# Pick the target that matches your agent and append it in place
loop-memory rules --agent codex    --write    # -> ./AGENTS.md
loop-memory rules --agent claude   --write    # -> ./CLAUDE.md
loop-memory rules --agent hermes   --write    # -> ./AGENTS.md
loop-memory rules --agent openclaw --write    # -> ./AGENTS.md
loop-memory rules --agent generic  --write    # -> ./AGENTS.md
```

### Safety properties

- The block is **appended after** your existing content; nothing
  before the marker line is touched.
- A `<!-- loop-memory:rules:installed -->` marker detects a prior
  install — re-running is a no-op unless you pass `--force`, in which
  case only the marker span is rewritten (manual edits **between** the
  two markers are preserved).
- The CLI exits 0 whether or not the target file exists (it creates an
  empty one) and whether or not `--agent` is given (it prints the
  generic block to stdout).

### Recommended two-phase setup

```bash
# Phase 1 — let the agent ingest its own past sessions.
loop-memory install-hooks
loop-memory hook --source codex --watch ~/.codex/sessions &

# Phase 2 — teach the agent the recall / wrap-up discipline so the
# captured memories actually get consulted next time.
loop-memory rules --agent codex --write
```

Pinned by 16 regression cases in `tests/test_cli_rules.py`. The
pattern is adopted from `2672243194/agentbrain` v0.4.3
(`agentbrain rules --write`).
