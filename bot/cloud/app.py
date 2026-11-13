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
from .due import GIVE_UP, due
from .engine import ACCOUNTS, ET, Engine

STATIC = Path(__file__).resolve().parent / "static"
REPO = os.environ.get("GITHUB_REPO") or CFG["cloud"]["repo"]
BRANCH = os.environ.get("LEDGER_BRANCH") or CFG["cloud"]["ledger_branch"]
CACHE = {"state": b"{}", "ledger": None}


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

    def say(self, kind, msg, ok=True):
        print(f"{datetime.now(ET):%Y-%m-%d %H:%M:%S} ET [{kind}] {msg}", flush=True)
        log = (self.eng.get("activity") or []) if self.eng else []
        self.eng.set("activity", ([{"ts": time.time(), "kind": kind, "msg": msg, "ok": ok}] + log)[:40])

    def snapshot(self):
        CACHE["ledger"] = json.dumps(self.eng.export(), separators=(",", ":")).encode()

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
        meta = {k: self.eng.get(k) for k in ("last_daily", "skip_day", "defer", "fills_day")}
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
            self.publish(now)  # show the dashboard before any slow work
        if self.restore == "none" and CACHE["ledger"] is None and now - self.t["first"] >= 600:
            self.t["first"] = now  # brand-new service: gather two weeks of posts, then save the first ledger
            if not self.collect(now)["failed"]:
                self.snapshot()
        d = self.work(now)
        if d["evening_day"] and now - self.t["daily"] >= 600:
            self.t["daily"] = now
            day, same = d["evening_day"], d["same_day"]
            if self.collect(now)["failed"] and same and hm < 19 * 60:
                return self.publish(now, force=True)  # wait for Reddit; after 7pm ET trade on what we have
            res = eng.daily(now if same else d["evening_ts"])
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
            elif not same:
                eng.set("skip_day", day)
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
        d = self.work(now)
        self.idle = not (d["evening_day"] or d["fills"]) and CACHE["ledger"] is not None
        self.publish(now)

    def publish(self, now, force=False):
        if not force and now - self.t["state"] < 20:
            return
        self.t["state"] = now
        st = self.eng.state(now)
        st.update(activity=self.eng.get("activity") or [], next_evening=next_evening(now),
                  boot={"restore": self.restore, "started": self.started, "idle": self.idle})
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
        elif path == "/api/state":
            self._send(200, CACHE["state"])
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


def main():
    threading.Thread(target=Runner().run, daemon=True).start()
    port = int(os.environ.get("PORT", "8792"))
    print(f"dashboard on :{port} (paper trading only; ledger branch {REPO}@{BRANCH})", flush=True)
    ThreadingHTTPServer((os.environ.get("HOST", "0.0.0.0"), port), Handler).serve_forever()


if __name__ == "__main__":
    main()
