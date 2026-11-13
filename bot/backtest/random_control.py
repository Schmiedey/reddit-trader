"""Do the cloud desk's rules beat simply buying random stocks Reddit is talking about?

Replays the cloud engine unchanged, except that each day every rule's real hits are moved to the same
number of random eligible stocks (4+ buyers in 5 days, tradable, passed the pump guard). Same sizing,
stops, cooldown and 10-day hold.

    uv run python -m bot.backtest.random_control [--seeds 5]
"""
import argparse
import json
import random

from ..config import DATA
from ..cloud import engine as E
from ..cloud import replay as R

SPLIT = "2025-08-14"


def patch(seed):
    rng = random.Random(seed)
    real_rule_hits, real_plan_exits = E.rule_hits, E.plan_exits
    buf = []

    def rule_hits(f, ret5):
        h = real_rule_hits(f, ret5)
        buf.append(h)  # the engine keeps this list object in its row; we refill it below
        return h

    def plan_exits(*a, **k):
        if buf:  # first call of the day comes after all rows are built and before any buys
            counts = {}
            for h in buf:
                for r in h:
                    counts[r] = counts.get(r, 0) + 1
                h.clear()
            for r, n in counts.items():
                for h in rng.sample(buf, n):
                    h.append(r)
            buf.clear()
        return real_plan_exits(*a, **k)

    E.rule_hits, E.plan_exits = rule_hits, plan_exits
    return lambda: (setattr(E, "rule_hits", real_rule_hits), setattr(E, "plan_exits", real_plan_exits))


def stats(res):
    out = {}
    for aid, r in res.items():
        c = r["curve"]
        i = max(k for k, x in enumerate(c) if x[0] < SPLIT)
        out[aid] = {"total": r["total"], "first": c[i][1] - 1, "later": c[-1][1] / c[i][1] - 1, "dd": r["dd"],
                    "avg": r["avg"], "trades": r["trades"]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    a = ap.parse_args()
    real = json.loads((R.STATIC / "backtest.json").read_text())
    start, end = real["start"], real["end"]
    bars, runs = None, []
    for s in range(a.seeds):
        undo = patch(s)
        try:
            eng, bars = R.run(start, end, park=False, bars=bars)
            runs.append(stats(R.summarize(eng)))
        finally:
            undo()
        print(f"seed {s}: " + "  ".join(f"{k} {v['total']:+.1%}" for k, v in runs[-1].items()), flush=True)
    base = stats(real["plain"])
    print(f"\n{'account':<12}{'window':<8}{'real':>8}{'random avg':>12}{'random range':>18}{'beats':>7}")
    for aid in base:
        for w in ("first", "later", "total"):
            rs = sorted(r[aid][w] for r in runs)
            print(f"{aid:<12}{w:<8}{base[aid][w]:>+8.1%}{sum(rs) / len(rs):>+12.1%}{rs[0]:>+9.1%} .. {rs[-1]:<+7.1%}"
                  f"{sum(base[aid][w] > x for x in rs):>4}/{len(rs)}")
    (DATA / "backtest" / "random_control.json").write_text(json.dumps({"real": base, "random": runs}, indent=1))


if __name__ == "__main__":
    main()
