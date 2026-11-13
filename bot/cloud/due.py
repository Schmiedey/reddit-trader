"""What work is due right now? Standard library only.

The Render free plan has a monthly hour budget, so the service sleeps most of the day. The GitHub
workflow runs this file against the saved ledger every 30 minutes to decide whether to wake it, and
the service calls the same function to decide what to do once awake.

    python3 bot/cloud/due.py ledger.json      # prints wake=1 or wake=0
"""
import json
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN, CLOSE, EVENING, GIVE_UP = 9 * 60 + 40, 16 * 60, 16 * 60 + 35, 18 * 60  # minutes after midnight, New York


def last_evening(now):
    """(timestamp, 'YYYY-MM-DD') of the most recent weekday 16:35 New York time at or before now."""
    run = datetime.fromtimestamp(now, ET).replace(hour=16, minute=35, second=0, microsecond=0)
    while run.timestamp() > now or run.weekday() >= 5:
        run = (run - timedelta(days=1)).replace(hour=16, minute=35)
    return run.timestamp(), run.date().isoformat()


def due(meta, has_pending, now=None):
    """meta: the ledger's saved markers. Returns what should run now:
    evening_day  the trading day whose evening run is still owed (today's, or a missed one), else None
    fills        True when yesterday's orders should be filled at today's open"""
    now = now or time.time()
    et = datetime.fromtimestamp(now, ET)
    today, hm = et.date().isoformat(), et.hour * 60 + et.minute
    ts, day = last_evening(now)
    done = (meta.get("last_daily") or "") >= day or meta.get("skip_day") == day
    deferred = meta.get("defer") == day and today == day  # no closing prices yet: try again tomorrow
    return {"evening_day": None if done or deferred else day, "evening_ts": ts, "same_day": day == today,
            "fills": et.weekday() < 5 and OPEN <= hm < CLOSE and bool(has_pending) and meta.get("fills_day") != today}


if __name__ == "__main__":
    try:
        led = json.load(open(sys.argv[1]))
    except (OSError, ValueError, IndexError):
        print("wake=1")  # no saved ledger yet: wake the service so it can create one
        raise SystemExit
    pending = any(o.get("status") == "pending" for a in led.get("accounts", {}).values() for o in a.get("orders", []))
    d = due(led.get("meta", {}), pending)
    print(f"wake={int(bool(d['evening_day'] or d['fills']))}")
