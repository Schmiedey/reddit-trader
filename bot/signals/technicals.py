"""Price checks that keep sentiment from buying at the worst moment."""


def atr_pct(bars, n=14):
    """Average true range over n days, as a fraction of the last close."""
    if len(bars) < n + 1:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs) / n / bars[-1]["c"]


def technicals(bars):
    if not bars or len(bars) < 21:
        return None
    c = [b["c"] for b in bars]
    return {
        "ret5": c[-1] / c[-6] - 1,
        "ret20": c[-1] / c[-21] - 1,
        "above_sma20": c[-1] > sum(c[-20:]) / 20,
        "atr_pct": atr_pct(bars),
    }


def entry_veto(t, tc):
    """Reason not to buy now even though sentiment is bullish, else None."""
    if t is None:
        return "under 21 days of price history"
    if t["ret5"] > tc["max_ret5"]:
        return f"already up {t['ret5']:.0%} in 5 days (chasing)"
    if not t["above_sma20"]:
        return "below its 20-day average (downtrend)"
    if t["atr_pct"] and t["atr_pct"] > tc["max_atr_pct"]:
        return f"too volatile (moves {t['atr_pct']:.1%} a day)"
    return None


def market_regime(spy_bars):
    """'risk-on' when SPY is above its 50-day average, else 'risk-off'."""
    if not spy_bars or len(spy_bars) < 50:
        return "unknown"
    c = [b["c"] for b in spy_bars]
    return "risk-on" if c[-1] > sum(c[-50:]) / 50 else "risk-off"
