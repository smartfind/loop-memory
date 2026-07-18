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

Save the following at `~/Library/LaunchAgents/com.loop-memory.watcher.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.loop-memory.watcher</string>
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
launchctl load ~/Library/LaunchAgents/com.loop-memory.watcher.plist
```

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
