#!/bin/bash
# Installs two background jobs (macOS launchd). Background QoS + nice 10 keeps them on
# efficiency cores. Undo with scripts/uninstall_launchd.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
[ -x "$PY" ] || { echo "Run 'uv sync' first"; exit 1; }
mkdir -p "$AGENTS" "$ROOT/data/logs"

make_plist() {
  local label=$1 module=$2 interval=$3 extra=${4:-}
  cat > "$AGENTS/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>$module</string>$extra
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartInterval</key><integer>$interval</integer>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>$ROOT/data/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/$label.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  echo "installed $label"
}

make_plist com.redditbot.collect bot.collect 900
make_plist com.redditbot.trade bot.trader.run_daily 1800 "<string>--scheduled</string>"
# Paper copy-trade test on public Hyperliquid wallets (crypto trades 24/7, so it runs whenever the Mac is awake).
make_plist com.redditbot.copytrade bot.copytrade.run 1800

# Keep the Mac from idle-sleeping 16:25-18:25 ET on weekdays so the daily trade run happens.
# (caffeinate -i can't stop lid-close sleep on battery.) Time converted to this Mac's timezone.
read -r H M <<<"$("$PY" -c "
from datetime import datetime; from zoneinfo import ZoneInfo
t = datetime.now(ZoneInfo('America/New_York')).replace(hour=16, minute=25).astimezone()
print(t.hour, t.minute)")"
CAL=""
for d in 1 2 3 4 5; do
  CAL+="<dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$H</integer><key>Minute</key><integer>$M</integer></dict>"
done
cat > "$AGENTS/com.redditbot.awake.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.redditbot.awake</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-t</string><string>7200</string>
  </array>
  <key>StartCalendarInterval</key><array>$CAL</array>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/com.redditbot.awake" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.redditbot.awake.plist"
echo "installed com.redditbot.awake (weekdays $H:$M local)"
