"""What work is due right now? Standard library only.

The Render free plan has a monthly hour budget, so the service sleeps most of the day. The GitHub
workflow runs this file against the saved ledger every 30 minutes to decide whether to wake it, and
the service calls the same function to decide what to do once awake.

    python3 bot/cloud/due.py ledger.json      # prints wake=1 or wake=0
"""
import json
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN, CLOSE, EVENING, GIVE_UP = 9 * 60 + 40, 16 * 60, 16 * 60 + 35, 18 * 60  # minutes after midnight, New York


def last_evening(now):
    """(timestamp, 'YYYY-MM-DD') of the most recent weekday 16:35 New York time at or before now."""
    run = datetime.fromtimestamp(now, ET).replace(hour=16, minute=35, second=0, microsecond=0)
    while run.timestamp() > now or run.weekday() >= 5:
        run = (run - timedelta(days=1)).replace(hour=16, minute=35)
    return run.timestamp(), run.date().isoformat()


def owed_evenings(meta, now, max_back=10):
    """Every weekday whose evening run has not happened yet, oldest first. GitHub's scheduler can go
    many hours (or a day) without waking the service, so a wake-up catches up on all of them in order."""
    done = meta.get("last_daily") or ""
    skips = set(meta.get("skip_days") or []) | {meta.get("skip_day")}
    d, days = date.fromisoformat(last_evening(now)[1]), []
    while len(days) < max_back and d.isoformat() > done:
        if d.weekday() < 5 and d.isoformat() not in skips:
            days.append(d.isoformat())
        if not done:
            break  # brand-new ledger: start with the latest day only
        d -= timedelta(days=1)
    return sorted(days)


def due(meta, has_pending, now=None):
    """meta: the ledger's saved markers. Returns what should run now:
    evening_day  the oldest trading day whose evening run is still owed (today's or a missed one), else None
    fills        True when yesterday's orders should be filled at today's open"""
    now = now or time.time()
    et = datetime.fromtimestamp(now, ET)
    today, hm = et.date().isoformat(), et.hour * 60 + et.minute
    owed = owed_evenings(meta, now)
    if owed and owed[0] == today and meta.get("defer") == today:
        owed = []  # no closing prices yet today: try again tomorrow
    day = owed[0] if owed else None
    ts = datetime.fromisoformat(day).replace(hour=16, minute=35, tzinfo=ET).timestamp() if day else None
    return {"evening_day": day, "evening_ts": ts, "same_day": day == today, "owed": len(owed),
            "fills": et.weekday() < 5 and OPEN <= hm < CLOSE and bool(has_pending) and meta.get("fills_day") != today}


def want_awake(now, owed, unsaved, hours_this_month, budget):
    """Should the service keep itself awake (ping itself) right now?

    Render's free plan puts a service to sleep after 15 minutes without requests, and GitHub's
    scheduler is too unreliable to wake it on time. So the service stays awake through the trading
    week (Sunday 6 pm to Friday 8 pm New York time) and does its own timing. It also stays awake while
    work is owed or its latest ledger has not been saved to GitHub yet. Past `budget` hours in a
    month (the free plan's 750 hours are shared with other services) it only stays up for owed work.
    """
    if owed:
        return True
    if hours_this_month >= budget:
        return False
    if unsaved:
        return True
    et = datetime.fromtimestamp(now, ET)
    wd, hm = et.weekday(), et.hour * 60 + et.minute
    if wd <= 3:
        return True                # Monday to Thursday, all day and night
    if wd == 4:
        return hm < 20 * 60        # Friday until 8 pm
    return wd == 6 and hm >= 18 * 60   # Sunday from 6 pm; Saturday asleep


if __name__ == "__main__":
    try:
        led = json.load(open(sys.argv[1]))
    except (OSError, ValueError, IndexError):
        print("wake=1")  # no saved ledger yet: wake the service so it can create one
        raise SystemExit
    pending = any(o.get("status") == "pending" for a in led.get("accounts", {}).values() for o in a.get("orders", []))
    d = due(led.get("meta", {}), pending)
    print(f"wake={int(bool(d['evening_day'] or d['fills']))}")
