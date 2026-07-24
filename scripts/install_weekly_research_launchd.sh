#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${SCRIPT_DIR:h}
WEEKDAY=${1:-1}
HOUR=${2:-3}
MINUTE=${3:-0}
LABEL=com.loopmemory.weekly-research
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNNER="$REPO_ROOT/scripts/weekly_research_update.sh"

if [[ ! "$WEEKDAY" =~ '^[0-7]$' ]] || (( HOUR < 0 || HOUR > 23 || MINUTE < 0 || MINUTE > 59 )); then
  echo "Usage: $0 [weekday 0-7] [hour 0-23] [minute 0-59]" >&2
  echo "Weekday 1 is Monday; 0 or 7 is Sunday." >&2
  exit 2
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.loop_memory/automation/logs"
chmod 700 "$HOME/.loop_memory" "$HOME/.loop_memory/automation" \
  "$HOME/.loop_memory/automation/logs" 2>/dev/null || true

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RUNNER</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>$WEEKDAY</integer>
    <key>Hour</key>
    <integer>$HOUR</integer>
    <key>Minute</key>
    <integer>$MINUTE</integer>
  </dict>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF
chmod 600 "$PLIST"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"

echo "Installed $LABEL for weekday $WEEKDAY at $(printf '%02d:%02d' "$HOUR" "$MINUTE") local time."
echo "Logs: $HOME/.loop_memory/automation/logs"
echo "Inspect: launchctl print gui/$UID/$LABEL"
