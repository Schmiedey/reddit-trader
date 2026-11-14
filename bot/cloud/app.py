"""Render entrypoint: one process, two jobs. PAPER TRADING ONLY (see engine.py: there is no broker).

1. Scheduler thread: fill orders after the open, read Reddit and run the trading job after the
   close, and keep a ledger snapshot that survives restarts. It does whatever is owed when it wakes.
2. Web server: the dashboard (/), its data (/api/state), the ledger (/api/ledger).

The free Render disk is wiped on every restart, so the ledger is saved to a git branch:
.github/workflows/desk.yml wakes the service when work is owed, waits for it to finish, then
commits /api/ledger. The service restores from that branch on boot. The repo is public, so no
tokens are needed. Between jobs the service sleeps, which keeps it inside Render's free hours.

    PORT=8792 uv run python -m bot.cloud.app          # run it locally
"""
import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import CFG
from .due import GIVE_UP, due, want_awake
from .engine import ACCOUNTS, ET, Engine

STATIC = Path(__file__).resolve().parent / "static"
REPO = os.environ.get("GITHUB_REPO") or CFG["cloud"]["repo"]
BRANCH = os.environ.get("LEDGER_BRANCH") or CFG["cloud"]["ledger_branch"]
CACHE = {"state": b"{}", "ledger": None}
COPY = {"body": None, "led": None, "ts": 0.0, "busy": False, "lock": threading.Lock()}
COPY_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/copy_ledger.json"
COPY_EVERY = 90  # seconds between catch-ups while someone has the page open


def refresh_copy():
    """Keep the copy desk live for the dashboard. The GitHub job saves the ledger, but GitHub runs it
    only every few hours, so this extends it to the present in memory: from the saved ledger when that
    is newer, otherwise from the last in-memory state. The replay is deterministic, so the next saved
    run shows the same numbers. Nothing here is saved."""
    from ..copydesk import desk as copydesk
    try:
        led = COPY["led"]
        try:
            with urllib.request.urlopen(f"{COPY_URL}?t={int(time.time())}", timeout=30) as r:
                saved = json.load(r)
            if led is None or saved["synced_to"] > led["synced_to"]:
                led = saved
        except Exception as e:  # noqa: BLE001
            print(f"copy ledger download failed: {type(e).__name__}", flush=True)
            if led is None:
                return
        if COPY["body"] is None:
            COPY["body"] = json.dumps(led, separators=(",", ":")).encode()
        led = json.loads(json.dumps(led))  # work on a copy: a failed catch-up leaves the last good state
        led, _ = copydesk.run(led, int(time.time() * 1000), min_step=60_000)
        COPY["led"] = led
        COPY["body"] = json.dumps(led, separators=(",", ":")).encode()
    except Exception as e:  # noqa: BLE001
        print(f"copy desk catch-up failed: {type(e).__name__}: {e}", flush=True)
    finally:
        COPY["ts"], COPY["busy"] = time.time(), False


def copy_body():
    with COPY["lock"]:
        if not COPY["busy"] and time.time() - COPY["ts"] > COPY_EVERY:
            COPY["busy"] = True
            threading.Thread(target=refresh_copy, daemon=True).start()
    if COPY["body"] is None:
        return None
    return b'{"updating":' + (b"true" if COPY["busy"] else b"false") + b',"server_time":' + str(int(time.time() * 1000)).encode() + b',"ledger":' + COPY["body"] + b"}"


def next_evening(now):
    d = datetime.fromtimestamp(now, ET)
    run = d.replace(hour=16, minute=35, second=0, microsecond=0)
    while run.timestamp() <= now or run.weekday() >= 5:
        run = (run + timedelta(days=1)).replace(hour=16, minute=35)
    return run.timestamp()


class Runner:
    def __init__(self):
        self.restore = "off" if os.environ.get("LEDGER_RESTORE") == "0" else "pending"
        self.t = defaultdict(float)
        self.started = time.time()
        self.eng = None
        self.idle = False
        self.want = True        # keep-awake decision, read by the pinger thread
        self.snap_ts = 0.0      # when the ledger snapshot served at /api/ledger was taken
        self.saved_ts = 0.0     # the newest snapshot GitHub is known to hold
        self.awake_acc = 0.0    # seconds awake not yet added to the monthly total

    def say(self, kind, msg, ok=True):
        print(f"{datetime.now(ET):%Y-%m-%d %H:%M:%S} ET [{kind}] {msg}", flush=True)
        log = (self.eng.get("activity") or []) if self.eng else []
        self.eng.set("activity", ([{"ts": time.time(), "kind": kind, "msg": msg, "ok": ok}] + log)[:40])

    def snapshot(self):
        led = self.eng.export()
        self.snap_ts = led["saved"]
        CACHE["ledger"] = json.dumps(led, separators=(",", ":")).encode()

    def check_saved(self, now):
        """Has GitHub's paper-ledger branch caught up with our latest snapshot? Anonymous read."""
        if self.snap_ts <= self.saved_ts or now - self.t["saved_check"] < 600:
            return
        self.t["saved_check"] = now
        try:
            url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/ledger.json?t={int(now)}"
            with urllib.request.urlopen(url, timeout=30) as r:
                self.saved_ts = max(self.saved_ts, float(json.load(r).get("saved") or 0))
        except Exception as e:
            print(f"saved-ledger check failed: {type(e).__name__}", flush=True)

    def track_awake(self, now):
        """Count this service's awake hours per month, to stay inside the shared free-plan hours."""
        dt = now - (self.t["awake_tick"] or now)
        self.t["awake_tick"] = now
        if 0 < dt < 120:
            self.awake_acc += dt
        if self.awake_acc >= 300:
            hours = self.eng.get("awake_hours") or {}
            month = datetime.fromtimestamp(now, ET).strftime("%Y-%m")
            hours = {m: h for m, h in hours.items() if m >= (datetime.fromtimestamp(now, ET) - timedelta(days=62)).strftime("%Y-%m")}
            hours[month] = round(hours.get(month, 0.0) + self.awake_acc / 3600, 3)
            self.eng.set("awake_hours", hours)
            self.awake_acc = 0.0

    def month_hours(self, now):
        return (self.eng.get("awake_hours") or {}).get(datetime.fromtimestamp(now, ET).strftime("%Y-%m"), 0.0)

    def decide_awake(self, now, d):
        want = want_awake(now, bool(d["evening_day"] or d["fills"]), self.snap_ts > self.saved_ts,
                          self.month_hours(now), BUDGET)
        if want != self.want and self.t["state"]:
            self.say("keepalive", "Staying awake" if want else "Allowed to sleep until the next wake-up")
        self.want = want

    def try_restore(self):
        url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/ledger.json"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                led = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                self.restore = "error"
                return print(f"ledger restore failed: HTTP {e.code}", flush=True)
            self.restore = "none"
            return self.say("start", "No saved ledger yet: starting three new $100,000 paper accounts")
        except Exception as e:
            self.restore = "error"
            return print(f"ledger restore failed: {type(e).__name__}", flush=True)
        self.eng.load(led)
        self.restore = "ok"
        self.snapshot()
        self.saved_ts = self.snap_ts  # same content as the copy on GitHub
        self.say("start", "Service restarted and restored its paper ledger")

    def collect(self, now):
        res = self.eng.collect(now, overlap_h=48, log=lambda m: print(m, flush=True))
        if res["failed"]:
            self.say("collect", f"Reddit archive did not answer for {', '.join('r/' + s for s in res['failed'])}", ok=False)
        else:
            self.say("collect", f"Read Reddit: {res['new']} new stock mentions")
        return res

    def work(self, now):
        """What is owed right now (see due.py)."""
        pending = any(b.pending() for b in self.eng.brokers().values())
        meta = {k: self.eng.get(k) for k in ("last_daily", "skip_day", "skip_days", "defer", "fills_day")}
        return due(meta, pending, now)

    def tick(self):
        eng, now = self.eng, time.time()
        et = datetime.fromtimestamp(now, ET)
        hm, today = et.hour * 60 + et.minute, et.date().isoformat()
        if self.restore in ("pending", "error"):  # never trade on an empty ledger by accident
            if now - self.t["restore"] >= 60:
                self.t["restore"] = now
                self.try_restore()
            if self.restore in ("pending", "error"):
                return
        if not self.t["state"]:
            self.set_idle(now)
            self.publish(now)  # show the dashboard before any slow work
        if self.restore == "none" and CACHE["ledger"] is None and now - self.t["first"] >= 600:
            self.t["first"] = now  # brand-new service: gather two weeks of posts, then save the first ledger
            if not self.collect(now)["failed"]:
                self.snapshot()
        d = self.work(now)
        if d["evening_day"] and now >= self.t["daily_wait"]:
            # One owed day per pass, oldest first, so a late wake-up replays every missed evening in order.
            day, same = d["evening_day"], d["same_day"]
            if now - (eng.get("collect_ok") or 0) > 1800 and self.collect(now)["failed"]:
                self.t["collect_fails"] += 1
                if self.t["collect_fails"] < 3 and not (same and hm >= 19 * 60):
                    self.t["daily_wait"] = now + 600  # wait for Reddit; after 3 tries (or 7pm ET) use what we have
                    return self.publish(now, force=True)
            self.t["collect_fails"] = 0
            res = eng.daily(now if same else d["evening_ts"])
            if not same and res["ok"]:
                self.say("evening", f"Caught up on the missed trading run for {day}")
            if res["ok"]:
                for aid, r in res["accounts"].items():
                    for e in r["events"]:
                        self.say("fill", f"{ACCOUNTS[aid]['name']}: {e}")
                    buys = ", ".join(r["buys"]) or "nothing new"
                    self.say("evening", f"{ACCOUNTS[aid]['name']}: ${r['equity']:,.0f}. Buying at the next open: {buys}")
                self.snapshot()
            elif same and hm >= GIVE_UP:
                eng.set("defer", day)
                self.say("evening", "No closing prices for today yet. Trying again tomorrow morning.", ok=False)
                self.snapshot()
            elif same:
                self.t["daily_wait"] = now + 600  # closing prices not published yet
            else:
                eng.set("skip_days", ((eng.get("skip_days") or []) + [day])[-30:])
                self.say("evening", f"The market was closed on {day}: no trading run")
                self.snapshot()
            d = self.work(now)
        if d["fills"] and not d["evening_day"] and now - self.t["fills"] >= 300:
            self.t["fills"] = now
            res = eng.morning_fills(now)
            if res["ok"]:
                eng.set("fills_day", today)
                for f in res["fills"]:
                    self.say("fill", f)
                self.snapshot()
        if now - self.t["mark"] >= 900 and now - ((eng.get("prices") or {}).get("ts") or 0) > 900:
            self.t["mark"] = now
            eng.mark(now)
        self.set_idle(now)
        self.track_awake(now)
        self.check_saved(now)
        self.decide_awake(now, self.work(now))
        self.publish(now)

    def set_idle(self, now):
        d = self.work(now)
        self.idle = not (d["evening_day"] or d["fills"]) and CACHE["ledger"] is not None

    def publish(self, now, force=False):
        if not force and now - self.t["state"] < 20:
            return
        self.t["state"] = now
        st = self.eng.state(now)
        st.update(activity=self.eng.get("activity") or [], next_evening=next_evening(now),
                  boot={"restore": self.restore, "started": self.started, "idle": self.idle},
                  awake={"keep": self.want, "hours": round(self.month_hours(now) + self.awake_acc / 3600, 1),
                         "budget": BUDGET, "saved": self.saved_ts or None, "unsaved": self.snap_ts > self.saved_ts})
        CACHE["state"] = json.dumps(st, separators=(",", ":")).encode()

    def run(self):
        self.eng = Engine()  # SQLite connections stay in this thread
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
                try:
                    self.say("error", traceback.format_exc().strip().splitlines()[-1][:160], ok=False)
                except Exception:
                    pass
            time.sleep(15)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/copy", "/copy.html"):
            self._send(200, (STATIC / "copy.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, CACHE["state"])
        elif path == "/api/copy":
            body = copy_body()
            self._send(200, body) if body else self._send(503, b'{"error":"loading","updating":true}')
        elif path == "/api/backtest":
            self._send(200, (STATIC / "backtest.json").read_bytes())
        elif path == "/api/ledger":
            self._send(200, CACHE["ledger"]) if CACHE["ledger"] else self._send(503, b'{"error":"ledger not ready"}')
        elif path == "/healthz":
            self._send(200, b'{"ok":true,"mode":"paper"}')
        else:
            self._send(404, b'{"error":"not found"}')

    do_HEAD = do_GET

    def log_message(self, *_):
        pass


def pinger(runner):
    """Keep the Render service awake by requesting its own public address (Render counts it as traffic)."""
    if not SELF_URL:
        return
    while True:
        time.sleep(540)
        if runner.want:
            try:
                urllib.request.urlopen(f"{SELF_URL.rstrip('/')}/healthz", timeout=30).read()
            except Exception as e:
                print(f"keep-awake ping failed: {type(e).__name__}", flush=True)


def main():
    runner = Runner()
    threading.Thread(target=runner.run, daemon=True).start()
    threading.Thread(target=pinger, args=(runner,), daemon=True).start()
    port = int(os.environ.get("PORT", "8792"))
    print(f"dashboard on :{port} (paper trading only; ledger branch {REPO}@{BRANCH})", flush=True)
    ThreadingHTTPServer((os.environ.get("HOST", "0.0.0.0"), port), Handler).serve_forever()


if __name__ == "__main__":
    main()
