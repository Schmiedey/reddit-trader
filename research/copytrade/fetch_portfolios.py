"""Fetch Hyperliquid portfolio (PnL history) for a random sample of leaderboard wallets. Cached per wallet."""
import json, random, time, os, sys, urllib.request
CACHE = os.path.join(os.path.dirname(__file__), "cache", "portfolio")
os.makedirs(CACHE, exist_ok=True)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2400
rows = json.load(open(os.path.join(os.path.dirname(__file__), "cache", "leaderboard.json")))["leaderboardRows"]
MINV = float(sys.argv[2]) if len(sys.argv) > 2 else 1e6
rows = [r for r in rows if float(dict(r["windowPerformances"])["allTime"]["vlm"]) > MINV]
random.seed(42); random.shuffle(rows)
addrs = [r["ethAddress"] for r in rows[:N]]

def post(body):
    req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            print('ERR', type(e).__name__, str(e)[:80], flush=True)
            time.sleep(3 * (attempt + 1))
    return None

done = 0
for a in addrs:
    f = os.path.join(CACHE, a + ".json")
    if os.path.exists(f):
        continue
    d = post({"type": "portfolio", "user": a})
    if d is not None:
        json.dump(d, open(f, "w"))
    done += 1
    if done % 50 == 0:
        print(done, flush=True)
    time.sleep(1.05)
print("finished", done)
