"""Candidate rules, fixed on 2026-11-12 after mining Aug-Sep 2025, late Dec 2025 and May-Nov 2026.

Months downloaded AFTER that (Oct-Nov 2025, Jan-Apr 2026) are a clean test: the rules were written
down before those months were looked at. For each rule: results on seen vs fresh months, a
month-by-month table, and a simple account simulation (10% of the account per trade, max 10 open).

    uv run python -m bot.backtest.candidates [--since 2025-08]
"""
import argparse
import json
from collections import defaultdict

import numpy as np

from ..db import DATA
from .explore import EXITS, atoms, fresh_idx, run, stats

# (name, conditions, result column, days held)
RULES = [
    ("Bounce A: 6+ talking, down 10%+ in 5d", ["6+ people talking", "no penny-sub posts", "down 10%+ in 5d"], "r5", 5),
    ("Bounce A, 3-day hold", ["6+ people talking", "no penny-sub posts", "down 10%+ in 5d"], "r3", 3),
    ("Bounce B: down 5% yesterday and 10% in 5d", ["no penny-sub posts", "down 5%+ yesterday", "down 10%+ in 5d"], "r5", 5),
    ("Bounce C: liquid, volatile, down 10% in 5d", ["big volume ($200M+)", "down 10%+ in 5d", "very volatile (ATR 8%+)"], "r5", 5),
    ("Bounce C, 3-day hold", ["big volume ($200M+)", "down 10%+ in 5d", "very volatile (ATR 8%+)"], "r3", 3),
    ("Buyers: attention 2x, 4+ buyers, opens flat", ["attention 2x", "4+ buyers", "opens flat"], "r5", 5),
    ("Buyers: 4+ buyers, opens flat (+30/-10, 10d)", ["4+ buyers", "opens flat"], "x30_10_10", 10),
    ("Baseline: any talked-about stock", ["3+ people talking"], "r5", 5),
]


def is_fresh(d):
    return ("2025-10-01" <= d < "2025-12-15") or ("2026-01-01" <= d < "2026-05-15")


def account(A, idx, col, hold, size=0.10, slots=10):
    """Monthly account returns: each trade gets `size` of the account while fewer than `slots` are open."""
    order = idx[np.argsort(A["i"][idx], kind="stable")]
    open_until, eq, month_eq, taken = [], 1.0, {}, 0
    pnl_at = defaultdict(float)
    days = sorted(set(A["i"].tolist()))
    by_day = defaultdict(list)
    for k in order:
        by_day[int(A["i"][k])].append(k)
    month_of = {}
    for k in range(len(A["i"])):
        month_of[int(A["i"][k])] = A["month"][k]
    for d in days:
        eq += pnl_at.pop(d, 0.0)
        open_until = [u for u in open_until if u > d]
        for k in by_day.get(d, []):
            r = A[col][k]
            if len(open_until) >= slots or np.isnan(r):
                continue
            open_until.append(d + hold)
            pnl_at[d + hold] += eq * size * r
            taken += 1
        month_eq[month_of[d]] = eq
    eq += sum(pnl_at.values())
    out, prev = {}, 1.0
    for m in sorted(month_eq):
        out[m] = round(month_eq[m] / prev - 1, 4)
        prev = month_eq[m]
    return out, taken, eq - 1


def main(since=None):
    A = run(since, morning=True)
    at = atoms(A)
    fresh = np.array([is_fresh(d) for d in A["date"]])
    res = {"rows": int(len(A["i"])), "fresh_rows": int(fresh.sum()), "rules": {}}
    print(f"{len(A['i'])} stock-days; fresh (never mined) stock-days: {int(fresh.sum())}")
    print(f"{'rule':<46}{'SEEN n':>7}{'mean':>7}{'win':>5} |{'FRESH n':>8}{'mean':>7}{'median':>8}{'win':>5}{'+10%':>6}{'-10%':>6}{'t':>6} |"
          f"{'acct/mo':>8}{'mo+':>6}{'worst mo':>9}{'trades/mo':>10}")
    for name, conds, col, hold in RULES:
        m = np.ones(len(A["i"]), dtype=bool)
        for c in conds:
            m &= at[c]
        seen = stats(A, fresh_idx(A, m & ~fresh, hold), col)
        fr = stats(A, fresh_idx(A, m & fresh, hold), col)
        idx = fresh_idx(A, m, hold)
        months, taken, total = account(A, idx, col, hold)
        mv = list(months.values())
        res["rules"][name] = {"conditions": conds, "seen": seen, "fresh": fr, "account_months": months,
                              "account_total": round(total, 4), "trades": taken}
        print(f"{name:<46}{seen['n']:>7}{seen.get('mean', 0):>+7.1%}{seen.get('win', 0):>5.0%} |{fr['n']:>8}{fr.get('mean', 0):>+7.1%}"
              f"{fr.get('median', 0):>+8.1%}{fr.get('win', 0):>5.0%}{fr.get('p10', 0):>6.0%}{fr.get('m10', 0):>6.0%}{fr.get('t', 0):>6.1f} |"
              f"{np.mean(mv):>+8.2%}{sum(v > 0 for v in mv):>3}/{len(mv):<2}{min(mv):>+9.1%}{taken / max(len(mv), 1):>10.1f}")
    (DATA / "backtest" / "candidates.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    main(ap.parse_args().since)
