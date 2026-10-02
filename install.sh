#!/bin/bash
# Installs ai-session-backup as a launchd job for the current user, running every 8 hours (03:30, 11:30, 19:30).
# Usage: ./install.sh [--skip artifacts,icloud] [--print-plist] [--uninstall]
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
label="com.${USER}.ai-session-backup"
backup="$HOME/Backups/ai-sessions"
plist="$HOME/Library/LaunchAgents/$label.plist"
skip=""
action="install"

while [ $# -gt 0 ]; do
  case "$1" in
    --skip) skip="${2:?--skip needs a value such as artifacts,icloud}"; shift 2 ;;
    --print-plist) action="print"; shift ;;
    --uninstall) action="uninstall"; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

render_plist() {
  local extra=""
  if [ -n "$skip" ]; then
    extra=$'\n\t\t<string>--skip</string>\n\t\t<string>'"$skip"'</string>'
  fi
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/bin/python3</string>
		<string>$backup/bin/ai_session_backup.py</string>$extra
	</array>
	<!-- Every 8 hours: 03:30, 11:30 and 19:30. If the Mac is asleep then, it runs as soon as it wakes. -->
	<key>StartCalendarInterval</key>
	<array>
		<dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>
		<dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>30</integer></dict>
		<dict><key>Hour</key><integer>19</integer><key>Minute</key><integer>30</integer></dict>
	</array>
	<key>StandardOutPath</key>
	<string>$backup/logs/launchd.out.log</string>
	<key>StandardErrorPath</key>
	<string>$backup/logs/launchd.err.log</string>
	<key>ProcessType</key>
	<string>Background</string>
	<key>LowPriorityIO</key>
	<true/>
	<key>Nice</key>
	<integer>10</integer>
</dict>
</plist>
EOF
}

case "$action" in
  print)
    render_plist
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$plist"
    echo "Removed $label. Your backups in $backup and in iCloud Drive were left in place."
    ;;
  install)
    if ! xcode-select -p >/dev/null 2>&1; then
      echo "/usr/bin/python3 needs the Xcode Command Line Tools: run xcode-select --install, then this script again." >&2
      exit 1
    fi
    mkdir -p "$backup/bin" "$backup/logs" "$(dirname "$plist")"
    ln -sfn "$here/ai_session_backup.py" "$backup/bin/ai_session_backup.py"
    render_plist > "$plist.tmp"
    plutil -lint "$plist.tmp" >/dev/null
    mv "$plist.tmp" "$plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "Installed $label. It runs at 03:30, 11:30 and 19:30."
    echo "Run it now:   launchctl kickstart gui/$(id -u)/$label"
    echo "Check it:     launchctl print gui/$(id -u)/$label"
    ;;
esac
