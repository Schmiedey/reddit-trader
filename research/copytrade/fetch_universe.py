"""Download compact position histories for a large wallet universe chosen WITHOUT looking at profit.

Universe: every leaderboard wallet with $50M-$2B lifetime volume (volume only, no PnL filter), not
already downloaded, in random order so a partial download is still a random sample. For each wallet,
main-exchange perp fills since 2026-04-21 (+ TWAP slices) are stored as [time, coin, position after
the fill], plus current positions. Wallets trading > ~33 fills/day are skipped after one page: their
history doesn't fit the API's 10k-fill window and they can't be copied anyway.

    python fetch_universe.py            # resumable; writes cache/uni/<addr>.json
"""
import glob, json, os, random, sys, time
from copysim import post
from persistence import ts

D = os.path.dirname(os.path.abspath(__file__)); C = os.path.join(D, "cache")
OUT = os.path.join(C, "uni"); os.makedirs(OUT, exist_ok=True)
START = int(ts("2026-04-21"))
main_dex = lambda c: not c.startswith(("@", "#")) and "/" not in c and ":" not in c


def candidates():
    rows = json.load(open(os.path.join(C, "leaderboard.json")))["leaderboardRows"]
    have = {os.path.basename(f)[:-5] for f in glob.glob(os.path.join(C, "long", "*.json"))}
    vol = lambda x: float(dict(x["windowPerformances"])["allTime"]["vlm"])
    out = sorted(x["ethAddress"] for x in rows if 5e7 <= vol(x) <= 2e9 and x["ethAddress"] not in have)
    random.Random(2026).shuffle(out)
    return out


def pages(kind, addr, until, first_only_if_hft=False):
    out, start = [], START
    while True:
        body = {"type": kind, "user": addr, "startTime": start, "endTime": until}
        if kind == "userFillsByTime":
            body["aggregateByTime"] = True
        r = post(body)
        if r is None:
            return out, "error"
        r = [x["fill"] if "fill" in x else x for x in r]
        out += r
        if len(r) < 2000:
            return out, "ok"
        span = (max(x["time"] for x in r) - min(x["time"] for x in r)) / 86400_000
        if first_only_if_hft and len(out) == 2000 and span < 60:
            return out, "hft"
        if len(out) >= 10000:
            return out, "truncated"
        start = max(x["time"] for x in r) + 1


def compact(fills):
    """[[time, coin, position after the fill], ...] and {coin: position before its first fill}."""
    seen, rows, first = set(), [], {}
    for x in sorted(fills, key=lambda x: (x["time"], x["tid"])):
        if main_dex(x["coin"]) and (x["tid"], x["time"]) not in seen:
            seen.add((x["tid"], x["time"]))
            post_pos = float(x["startPosition"]) + (1 if x["side"] == "B" else -1) * float(x["sz"])
            rows.append([x["time"], x["coin"], round(post_pos, 8)])
            first.setdefault(x["coin"], float(x["startPosition"]))
    return rows, first


if __name__ == "__main__":
    addrs = candidates()
    print("universe", len(addrs), flush=True)
    done = 0
    for i, a in enumerate(addrs):
        f = os.path.join(OUT, a + ".json")
        if os.path.exists(f):
            continue
        asof = int(time.time() * 1000)
        fills, status = pages("userFillsByTime", a, asof, first_only_if_hft=True)
        rec = {"asof": asof, "status": status}
        if status == "ok":
            twap, s2 = pages("userTwapSliceFillsByTime", a, asof)
            st = post({"type": "clearinghouseState", "user": a})
            if s2 != "ok" or st is None:
                rec["status"] = "error"
            else:
                rec["rows"], rec["first"] = compact(fills + twap)
                rec["pos"] = {p["position"]["coin"]: float(p["position"]["szi"]) for p in st["assetPositions"]
                              if main_dex(p["position"]["coin"])}
        json.dump(rec, open(f, "w"), separators=(",", ":"))
        done += 1
        if done % 100 == 0:
            n = len(glob.glob(os.path.join(OUT, "*.json")))
            print(f"{time.strftime('%H:%M')} {n}/{len(addrs)}", flush=True)
    print("finished", flush=True)
