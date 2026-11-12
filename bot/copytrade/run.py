"""Paper copy-trader for public Hyperliquid wallets. No orders are placed anywhere.

  python -m bot.copytrade.run            one cycle: fetch positions, update paper accounts
  python -m bot.copytrade.run --report   scorecard: picked wallets vs random control

Wallet list: research/copytrade/wallets.json  ({"picks": [addr...], "control": [addr...]}).
State: data/copytrade.db
"""
import json, sqlite3, statistics, sys, time, urllib.request
from pathlib import Path

from bot.copytrade import core

ROOT = Path(__file__).resolve().parents[2]
WALLETS = ROOT / "research" / "copytrade" / "wallets.json"
DB = ROOT / "data" / "copytrade.db"
API = "https://api.hyperliquid.xyz/info"


def post(body, tries=3):
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def wallet_state(addr):
    """(positions {coin: signed size}, equity in USD) or (None, 0) if the fetch failed.

    Equity = perp account value + spot USDC not already held as perp margin, because unified
    accounts keep their collateral on the spot side.
    """
    perp = post({"type": "clearinghouseState", "user": addr})
    spot = post({"type": "spotClearinghouseState", "user": addr})
    if perp is None or spot is None:
        return None, 0.0
    pos = {p["position"]["coin"]: float(p["position"]["szi"]) for p in perp["assetPositions"]}
    equity = float(perp["marginSummary"]["accountValue"])
    for b in spot.get("balances", []):
        if b["coin"] == "USDC":
            equity += max(float(b["total"]) - float(b["hold"]), 0.0)
    return pos, equity


def connect():
    DB.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript("""
      create table if not exists accounts(addr text primary key, grp text, started real, updated real, state text);
      create table if not exists trades(ts real, addr text, coin text, delta real, px real, cost real);
      create table if not exists equity_log(ts real, addr text, equity real, wallet_equity real, lev real);
    """)
    return db


def cycle():
    groups = json.loads(WALLETS.read_text())
    mids = post({"type": "allMids"})
    if not mids:
        print("no prices, skipping cycle"); return
    mids = {k: float(v) for k, v in mids.items()}
    db, now, n_trades, failed = connect(), time.time(), 0, 0
    for grp, addrs in groups.items():
        for addr in addrs:
            row = db.execute("select state from accounts where addr=?", (addr,)).fetchone()
            acct = json.loads(row[0]) if row else core.new_account()
            if not row:
                db.execute("insert into accounts values(?,?,?,?,?)", (addr, grp, now, now, json.dumps(acct)))
            pos, weq = (None, 0.0) if acct["blown"] else wallet_state(addr)
            failed += pos is None and not acct["blown"]
            trades = core.step(acct, pos, weq, mids)
            db.executemany("insert into trades values(?,?,?,?,?,?)", [(now, addr, *t) for t in trades])
            db.execute("insert into equity_log values(?,?,?,?,?)", (now, addr, acct["equity"], weq, core.gross_leverage(acct)))
            db.execute("update accounts set updated=?, state=? where addr=?", (now, json.dumps(acct), addr))
            db.commit()
            n_trades += len(trades)
            time.sleep(0.15)
    print(f"{time.strftime('%Y-%m-%d %H:%M')} cycle done: {n_trades} paper trades, {failed} fetch failures")


def report():
    db = connect()
    rows = db.execute("select addr, grp, started, updated, state from accounts").fetchall()
    if not rows:
        print("no data yet"); return
    days = (max(r[3] for r in rows) - min(r[2] for r in rows)) / 86400
    print(f"Paper copy test, {days:.1f} days running. Each account started with ${core.START_EQUITY:,.0f}.\n")
    print(f"{'group':8s} {'n':>3s} {'mean':>8s} {'median':>8s} {'profitable':>10s} {'blown':>5s}")
    detail = []
    for grp in sorted({r[1] for r in rows}):
        accts = [(r[0], json.loads(r[4])) for r in rows if r[1] == grp]
        roi = [a["equity"] / core.START_EQUITY - 1 for _, a in accts]
        print(f"{grp:8s} {len(roi):3d} {statistics.mean(roi):+8.1%} {statistics.median(roi):+8.1%} "
              f"{sum(x > 0 for x in roi) / len(roi):10.0%} {sum(a['blown'] for _, a in accts):5d}")
        detail += [(grp, addr, a) for addr, a in accts]
    for coin in ("BTC", "ETH", "SOL"):
        sides = [a["pos"][coin]["size"] > 0 for g, _, a in detail if g == "picks" and coin in a["pos"]]
        if sides:
            print(f"picks holding {coin}: {sum(sides)} long, {len(sides) - sum(sides)} short")
    print()
    for grp, addr, a in sorted(detail, key=lambda d: (d[0] != "picks", -d[2]["equity"])):
        n = db.execute("select count(*) from trades where addr=?", (addr,)).fetchone()[0]
        print(f"  {grp:8s} {addr}  {a['equity'] / core.START_EQUITY - 1:+7.1%}  lev={core.gross_leverage(a):4.1f}  "
              f"trades={n:4d}{'  BLOWN' if a['blown'] else ''}")


if __name__ == "__main__":
    report() if "--report" in sys.argv else cycle()
