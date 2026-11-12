import os
import time
from datetime import datetime, timezone

import psutil

from .config import CFG

_proc = psutil.Process()


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def lower_priority():
    try:
        os.nice(10)
    except OSError:
        pass
    _proc.cpu_percent(interval=None)  # prime the counter


def breathe():
    """Sleep if this process has been using more CPU than the configured limit since the last call."""
    limit = CFG["runtime"]["cpu_limit_pct"]
    cpu = _proc.cpu_percent(interval=None)
    if cpu > limit:
        time.sleep(min(5.0, 0.5 + (cpu / limit - 1)))


def parse_iso(s):
    if not s:
        return time.time()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def parse_twitter_date(s):
    if not s:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    try:
        return parse_iso(s)
    except Exception:
        return None


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
