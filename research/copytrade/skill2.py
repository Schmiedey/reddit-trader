"""Find wallets with real timing skill across thousands of wallets, and test the consensus they make.

Council method, fixed before results:
  universe  409 clean wallets (chosen on data up to Jul 31) + the volume-only universe (fetch_universe.py)
  skill     per wallet, daily samples of (direction held) x (next-24h return minus the coin's drift) in
            BTC/ETH/SOL/HYPE; t-stat, plus a placebo p-value from 1000 random hourly time-shifts of the
            wallet's own positions (keeps how long it holds, breaks its timing). Benjamini-Hochberg across wallets.
  windows   A = Apr 21-Jun 30 (estimate skill) -> B = Jul 1-Aug 15 (choose rule) -> TEST = Aug 16-Nov 12
            (once; skill re-estimated on A+B with the chosen rule).
  rules     which wallets vote x threshold, chosen on B by Sharpe:
            t>=2 | FDR q<=0.2 | top 50 by t | top 200 by t | all with t>1 ; threshold 0.05 / 0.1 / 0.2
  trading   as council.py: 25% per coin, next-hour execution, 0.095% per side, funding.

    python skill2.py
"""
import glob, json, os, sys
import numpy as np
import council as K

C = K.C
H = 3600_000
iA0, iB0, iT0 = 0, int(np.searchsorted(K.GRID, K.steady.ts("2026-07-01"))), K.MID
rng = np.random.default_rng(11)


def load_universe():
    z = np.load(os.path.join(C, "council_pos.npz")); POS, addrs = z["pos"], list(z["addrs"])
    import fetch_long
    clean = set(fetch_long.pool())
    keep = [i for i, a in enumerate(addrs) if a in clean]
    POS, addrs = list(POS[keep]), [addrs[i] for i in keep]
    src = ["clean"] * len(addrs)
    n_hft = n_err = 0
    for f in sorted(glob.glob(os.path.join(C, "uni", "*.json"))):
        r = json.load(open(f))
        if r["status"] != "ok":
            n_hft += r["status"] == "hft"; n_err += r["status"] != "hft"; continue
        grid = np.zeros((len(K.COINS), K.N), dtype=np.float32)
        for ci, c in enumerate(K.COINS):
            ev = [(t, p) for t, cc, p in r["rows"] if cc == c]
            if ev:
                ts = np.array([e[0] for e in ev]); ps = np.array([e[1] for e in ev])
                idx = np.searchsorted(ts, K.GRID, side="right") - 1
                grid[ci] = np.where(idx >= 0, ps[np.clip(idx, 0, None)], r["first"].get(c, 0.0))
            else:
                grid[ci] = r["pos"].get(c, 0.0)
        POS.append(grid); addrs.append(os.path.basename(f)[:-5]); src.append("uni")
    print(f"universe: {src.count('clean')} clean + {src.count('uni')} volume-only wallets "
          f"(skipped {n_hft} high-frequency, {n_err} errors)")
    return np.array(POS), addrs, np.array(src)


def daily_samples(POS, P, lo, hi):
    r24 = np.full_like(P, np.nan); r24[:, :-24] = P[:, 24:] / P[:, :-24] - 1
    days = np.arange(lo, hi - 24, 24)
    ex = r24[:, days] - np.nanmean(r24[:, days], axis=1, keepdims=True)
    return np.sign(POS[:, :, days]), ex                                   # (w, c, d), (c, d)


def skill(POS, P, lo, hi, n_shift=1000):
    """t-stat of timing, plus a placebo p-value: the wallet's own position history shifted by a random
    number of HOURS (>= 1 week, wrapping inside the window) against the same price path. Keeps how
    long and how often it holds; breaks its timing. 1000 shifts -> p-values down to 0.001."""
    sg, ex = daily_samples(POS, P, lo, hi)
    x = sg * ex[None]; on = sg != 0
    n = on.sum((1, 2)); m = x.sum((1, 2)) / np.maximum(n, 1)
    var = ((x - m[:, None, None]) ** 2 * on).sum((1, 2)) / np.maximum(n - 1, 1)
    t = np.where(n >= 20, m / np.sqrt(var / np.maximum(n, 1) + 1e-18), 0.0)
    days = np.arange(lo, hi - 24, 24); span = hi - lo
    S = np.sign(POS[:, :, lo:hi]).astype(np.int8)
    beat = np.zeros(len(m))
    for _ in range(n_shift):
        k = rng.integers(168, span - 168)
        idx = (days - lo - k) % span
        sh = S[:, :, idx]
        ms = (sh * ex[None]).sum((1, 2)) / np.maximum((sh != 0).sum((1, 2)), 1)
        beat += ms >= m
    p = (beat + 1) / (n_shift + 1)
    return t, np.where(n >= 20, p, 1.0), n


def bh(p, q):
    """Benjamini-Hochberg: which p-values are discoveries at false-discovery rate q. Only wallets with
    enough data to test (p < 1) count as tests; untestable ones would inflate the correction."""
    m = max(int((p < 1).sum()), 1)
    order = np.argsort(p); k = np.arange(1, len(p) + 1)
    ok = p[order] <= q * k / m
    cut = k[ok].max() if ok.any() else 0
    out = np.zeros(len(p), bool); out[order[:cut]] = True
    return out


def weights(rule, t, p):
    if rule == "t>=2": sel = t >= 2
    elif rule == "FDR q<=0.2": sel = bh(p, 0.2) & (t > 0)
    elif rule == "top 50": sel = np.zeros(len(t), bool); sel[np.argsort(-t)[:50]] = True; sel &= t > 0
    elif rule == "top 200": sel = np.zeros(len(t), bool); sel[np.argsort(-t)[:200]] = True; sel &= t > 0
    elif rule == "all t>1": sel = t > 1
    return np.where(sel, t, 0.0)


RULES = ["t>=2", "FDR q<=0.2", "top 50", "top 200", "all t>1"]
THETAS = [0.05, 0.1, 0.2]


def main():
    POS, addrs, src = load_universe()
    P, F = K.prices(), K.funding_grid()
    base = K.backtest(np.ones((len(K.COINS), K.N)), P, F)
    r7 = np.zeros_like(P); r7[:, 168:] = P[:, 168:] / P[:, :-168] - 1
    trend = K.backtest(np.sign(r7), P, F)

    # stage 1: skill on A, choose rule + threshold on B
    tA, pA, nA = skill(POS, P, iA0, iB0)
    print(f"\nA (Apr 21-Jun 30): {int((nA >= 20).sum())} wallets with >= 20 daily samples | t>=2: {int((tA >= 2).sum())} "
          f"(luck ~{0.023 * (nA >= 20).sum():.0f}) | FDR q<=0.2 discoveries: {int((bh(pA, 0.2) & (tA > 0)).sum())}")
    print("choosing on B (Jul 1-Aug 15), Sharpe of each rule x threshold:")
    best, best_s = None, -9
    for rule in RULES:
        w = weights(rule, tA, pA)
        row = []
        for th in THETAS:
            r = K.backtest(K.consensus_hold(POS, w, th), P, F)
            s = K.summary(r, iB0, iT0)
            row.append(f"{th}: {s['ret']:+6.1%} (S {s['sharpe']:+.1f})")
            if (w > 0).sum() >= 3 and s["sharpe"] > best_s:
                best, best_s = (rule, th), s["sharpe"]
        print(f"  {rule:11s} [{int((w > 0).sum()):4d} wallets]  " + " | ".join(row))
    print(f"chosen: {best[0]} at threshold {best[1]} (B Sharpe {best_s:.2f})")

    # stage 2: re-estimate on A+B, test once
    t, p, n = skill(POS, P, iA0, iT0)
    w = weights(best[0], t, p)
    print(f"\nA+B skill (Apr 21-Aug 15): {int((w > 0).sum())} wallets vote "
          f"({int(((w > 0) & (src == 'clean')).sum())} clean, {int(((w > 0) & (src == 'uni')).sum())} volume-only); "
          f"FDR q<=0.2 discoveries: {int((bh(p, 0.2) & (t > 0)).sum())}, q<=0.05: {int((bh(p, 0.05) & (t > 0)).sum())}")
    strat = K.backtest(K.consensus_hold(POS, w, best[1]), P, F)
    elig = np.where(n >= 20)[0]
    fakes = []
    for _ in range(200):
        fw = np.zeros(len(w)); pick = rng.choice(elig, int((w > 0).sum()), replace=False)
        fw[pick] = rng.permutation(w[w > 0]); fakes.append(K.backtest(K.consensus_hold(POS, fw, best[1]), P, F))
    allw = K.backtest(K.consensus_hold(POS, (n >= 20).astype(float), best[1]), P, F)
    ok = K.verdict(f"{best[0]} @ {best[1]}", strat, base, fakes, P, (("trend rule", trend), ("all-wallet consensus", allw)))
    # reference: every rule on TEST (not used for choosing)
    print("\n  for reference only, every rule on TEST:")
    for rule in RULES:
        ww = weights(rule, t, p)
        print(f"    {rule:11s} [{int((ww > 0).sum()):4d}] " + " | ".join(
            f"{th}: {K.summary(K.backtest(K.consensus_hold(POS, ww, th), P, F), iT0, K.N)['ret']:+.1%}" for th in THETAS))
    top = np.argsort(-t)[:15]
    json.dump({"rule": best[0], "threshold": best[1], "passes": bool(ok),
               "weights": {addrs[i]: round(float(w[i]), 3) for i in np.where(w > 0)[0]},
               "top15": [{"addr": addrs[i], "t": round(float(t[i]), 2), "p": round(float(p[i]), 4), "src": src[i]} for i in top]},
              open(os.path.join(C, "skill2_result.json"), "w"), indent=1)
    print("\n  top 15 wallets by skill (A+B):")
    for i in top:
        print(f"    {addrs[i]}  t={t[i]:.2f}  placebo p={p[i]:.3f}  samples {int(n[i])}  [{src[i]}]")


if __name__ == "__main__":
    main()
