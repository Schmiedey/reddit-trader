#!/bin/bash
for label in com.redditbot.collect com.redditbot.trade com.redditbot.awake com.redditbot.copytrade; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
  echo "removed $label"
done
