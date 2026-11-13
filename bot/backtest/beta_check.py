"""Is the cloud desk's backtest edge just extra market risk? Regress each account's daily returns on
SPY, a high-beta ETF (SPHB) and small caps (IWM), and compare with a benchmark held at the same beta.

    uv run python -m bot.backtest.beta_check
Reads bot/cloud/static/backtest.json (from `bot.cloud.replay --save`). Benchmark prices are cached.
"""
import json
from pathlib import Path

import numpy as np

from ..config import DATA

BT = Path(__file__).resolve().parents[1] / "cloud" / "static" / "backtest.json"
CACHE = DATA / "backtest" / "bench.json"
BENCH = ["SPY", "SPHB", "IWM", "ARKK"]
SPLIT = "2025-08-14"


def closes(start, end):
    if CACHE.exists():
        c = json.loads(CACHE.read_text())
        if c.get("start") == start and c.get("end") == end:
            return c["px"]
    import yfinance as yf
    df = yf.download(BENCH, start=start, end=end, auto_adjust=True, progress=False)["Close"]
    px = {s: {d.strftime("%Y-%m-%d"): float(v) for d, v in df[s].dropna().items()} for s in BENCH}
    CACHE.write_text(json.dumps({"start": start, "end": end, "px": px}))
    return px


def ols(y, x):
    X = np.column_stack([np.ones(len(x)), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ coef
    s2 = res @ res / (len(y) - X.shape[1])
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    return coef, coef / se


def window(days, eq, px, sym, lo, hi):
    idx = [i for i in range(1, len(days)) if lo < days[i] <= hi and days[i] in px[sym] and days[i - 1] in px[sym]]
    ra = np.array([eq[i] / eq[i - 1] - 1 for i in idx])
    rb = np.array([px[sym][days[i]] / px[sym][days[i - 1]] - 1 for i in idx])
    (a, b), (ta, _) = ols(ra, rb)
    acct = np.prod(1 + ra) - 1
    bench = np.prod(1 + rb) - 1
    matched = np.prod(1 + b * rb) - 1  # the benchmark held at the account's beta, rest in cash
    return {"beta": b, "alpha_yr": a * 252, "t": ta, "acct": acct, "bench": bench, "matched": matched,
            "excess": acct - matched, "corr": np.corrcoef(ra, rb)[0, 1]}


def main():
    bt = json.loads(BT.read_text())
    px = closes("2024-08-01", "2026-11-14")
    wins = [("Unseen year", "", SPLIT), ("Later 15 mo", SPLIT, "9999"), ("All 27 mo", "", "9999")]
    out = {}
    for aid, r in bt["plain"].items():
        days = [c[0] for c in r["curve"]]
        eq = [c[1] for c in r["curve"]]
        out[aid] = {}
        print(f"\n{r['name']}")
        print(f"{'window':<13}{'bench':<6}{'beta':>6}{'corr':>6}{'acct':>9}{'bench':>9}{'@beta':>9}{'excess':>9}{'alpha/yr':>10}{'t':>6}")
        for wname, lo, hi in wins:
            for sym in BENCH:
                w = window(days, eq, px, sym, lo, hi)
                out[aid][f"{wname}|{sym}"] = {k: round(float(v), 4) for k, v in w.items()}
                print(f"{wname:<13}{sym:<6}{w['beta']:>6.2f}{w['corr']:>6.2f}{w['acct']:>+9.1%}{w['bench']:>+9.1%}"
                      f"{w['matched']:>+9.1%}{w['excess']:>+9.1%}{w['alpha_yr']:>+10.1%}{w['t']:>6.1f}")
    (DATA / "backtest" / "beta_check.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
