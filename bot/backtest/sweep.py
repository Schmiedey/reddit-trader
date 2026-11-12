"""Search for the most profitable rule set without fooling ourselves.

Days are split in time: configs are chosen on the older TRAIN part only, then the winners are
re-run on the newer TEST part they never saw. Controls run on the same days:
SPY buy-and-hold, the same rules buying RANDOM tradable names, and the INVERSE (contrarian) score.
If the sentiment score can't beat random picks out of sample, it isn't adding anything.

    uv run python -m bot.backtest.sweep --variant base --n 300
"""
import argparse
import json
import random
import time

from ..db import DATA
from .build import bt_connect, load_bars
from .sim import Bars, load_signals, signal_study, simulate

OUT = DATA / "backtest"

SPACE = {
    "scoring.buy_threshold": [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4],
    "scoring.min_authors": [3, 5, 8, 12],
    "scoring.exit_threshold": [-0.3, -0.15, 0.0, -9],
    "trading.max_hold_days": [2, 3, 5, 10, 20],
    "trading.atr_stop_mult": [1.0, 1.5, 2.0, 3.0],
    "trading.max_stop_pct": [0.08, 0.12, 0.2],
    "trading.reward_risk": [1.0, 1.5, 2.0, 3.0, 50],
    "trading.max_positions": [5, 10, 15],
    "trading.max_new_per_day": [1, 3, 5],
    "trading.position_pct": [0.05, 0.1],
    "trading.risk_per_trade_pct": [0.01, 0.02],
    "trading.max_ret5": [0.1, 0.25, 9],
    "trading.risk_off_max_new": [0, 1, 3],
    "trading.risk_off_threshold_bump": [0.0, 0.1],
}
FLAGS = {"use_regime": [True, False], "use_veto": [True, False], "use_pump": [True, False]}
MODES = ["score", "score", "score", "attention", "inverse"]  # strategies, not just settings
WEIGHTS = [None, {"crowd": 1.0, "buzz": 0.0}, {"crowd": 0.5, "buzz": 0.5}, {"crowd": 0.0, "buzz": 1.0},
           {"crowd": 0.7, "buzz": -0.3}]


def objective(m, min_trades):
    if not m.get("trades") or m["trades"] < min_trades:
        return -99
    return m["sharpe"]


def run(variant, n, seed=0, train_frac=0.6, top=10):
    con = bt_connect()
    signals = load_signals(con, variant)
    if not signals:
        raise SystemExit(f"no signals for variant {variant!r}: run bot.backtest.build first")
    B = Bars(load_bars(con))
    days = sorted(d for d in signals if d in set(B.dates["SPY"]))
    cut = days[int(len(days) * train_frac)]
    tr, te = (days[0], days[int(len(days) * train_frac) - 1]), (cut, days[-1])
    min_trades = max(20, len(days) // 20)
    rng = random.Random(seed)
    t0 = time.time()

    default = {"name": "live config", "over": {}, "mode": None}
    configs = [default] + [{
        "name": f"cfg{i}",
        "over": {k: rng.choice(v) for k, v in SPACE.items()},
        "flags": {k: rng.choice(v) for k, v in FLAGS.items()},
        "weights": rng.choice(WEIGHTS),
        "mode": rng.choice(MODES),
    } for i in range(n)]
    results = []
    for c in configs:
        c["train"] = simulate(signals, B, c["over"], *tr, mode=c.get("mode"), weights=c.get("weights"),
                              **c.get("flags", {}))
        results.append(c)
    results.sort(key=lambda c: objective(c["train"], min_trades * train_frac), reverse=True)
    finalists = [default] + [c for c in results if c is not default][:top]
    for c in finalists:
        c["test"] = simulate(signals, B, c["over"], *te, mode=c.get("mode"), weights=c.get("weights"),
                             **c.get("flags", {}))
        # same rules, random names (5 seeds) and the contrarian ranking, on the test days
        rnd = [simulate(signals, B, c["over"], *te, mode="random", seed=s, weights=c.get("weights"),
                        **c.get("flags", {})) for s in range(5)]
        c["test_random_mean"] = {k: round(sum(r[k] for r in rnd) / 5, 4) for k in ("return", "sharpe", "max_dd")}
        c["test_inverse"] = simulate(signals, B, c["over"], *te, mode="inverse", weights=c.get("weights"),
                                     **c.get("flags", {}))
    study = {"train": signal_study({d: signals[d] for d in days if d < cut}, B),
             "test": signal_study({d: signals[d] for d in days if d >= cut}, B)}
    OUT.mkdir(parents=True, exist_ok=True)
    out = {"variant": variant, "train": tr, "test": te, "configs_tried": len(configs), "min_trades": min_trades,
           "seconds": round(time.time() - t0), "signal_study": study,
           "finalists": [{k: c.get(k) for k in ("name", "mode", "over", "flags", "weights", "train", "test",
                                                "test_random_mean", "test_inverse")} for c in finalists],
           "all_train": [{"name": c["name"], "sharpe": c["train"].get("sharpe"), "return": c["train"].get("return"),
                          "trades": c["train"].get("trades")} for c in results]}
    (OUT / f"sweep_{variant}.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def summarize(out):
    print(f"variant {out['variant']}: train {out['train'][0]}..{out['train'][1]}, test {out['test'][0]}..{out['test'][1]}, "
          f"{out['configs_tried']} configs, {out['seconds']}s")
    for split in ("train", "test"):
        s = out["signal_study"][split]
        print(f"  signal {split}: n={s['n']} IC1={s.get('ic1')} IC5={s.get('ic5')} IC10={s.get('ic10')}")
        if "h5" in s:
            print("    5d excess by score quintile: " + "  ".join(f"{b['mean_excess']:+.2%}" for b in s["h5"]))
    print(f"  {'name':<12}{'train ret':>10}{'sharpe':>8}{'trades':>7} | {'test ret':>9}{'spy':>8}{'sharpe':>8}"
          f"{'maxdd':>8}{'trades':>7}{'random':>8}{'inverse':>8}")
    for c in out["finalists"]:
        a, b = c["train"], c["test"]
        print(f"  {c['name'] + ' ' + (c.get('mode') or 'score')[:3]:<12}{a.get('return', 0):>10.1%}{a.get('sharpe', 0):>8.2f}{a.get('trades', 0):>7} | "
              f"{b.get('return', 0):>9.1%}{b.get('spy_return', 0):>8.1%}{b.get('sharpe', 0):>8.2f}"
              f"{b.get('max_dd', 0):>8.1%}{b.get('trades', 0):>7}{c['test_random_mean']['return']:>8.1%}"
              f"{c['test_inverse'].get('return', 0):>8.1%}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="base")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    summarize(run(a.variant, a.n, a.seed))


if __name__ == "__main__":
    main()
