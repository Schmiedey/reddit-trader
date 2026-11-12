def stop_and_target(atr_pct, tc):
    """Stop 2 ATRs below entry (clamped 4–12%), target at reward_risk times that distance."""
    stop = tc["min_stop_pct"] if not atr_pct else min(tc["max_stop_pct"], max(tc["min_stop_pct"], tc["atr_stop_mult"] * atr_pct))
    return stop, stop * tc["reward_risk"]


def position_size(equity, price, pct, stop_pct=None, risk_pct=None):
    """Whole shares. Capped at pct of equity, and (if given) sized so hitting the stop loses risk_pct of equity."""
    if price <= 0:
        return 0
    cap = equity * pct
    if stop_pct and risk_pct:
        cap = min(cap, equity * risk_pct / stop_pct)
    return int(cap // price)


def plan_entries(signals, held, tc, sc, regime="risk-on"):
    threshold = sc["buy_threshold"] + (tc["risk_off_threshold_bump"] if regime == "risk-off" else 0)
    max_new = tc["risk_off_max_new"] if regime == "risk-off" else tc["max_new_per_day"]
    picks = [
        s for s in signals.values()
        if not s.get("thin") and not s.get("blocked") and not s.get("veto") and s["ticker"] not in held
        and s["score"] >= threshold and len(s["agree"]) >= sc["min_agreeing_sources"]
    ]
    picks.sort(key=lambda s: s["score"], reverse=True)
    return picks[: max(0, min(max_new, tc["max_positions"] - len(held)))]


def plan_exits(held_days, signals, tc, sc):
    """held_days: {symbol: trading days held or None}. Returns [(symbol, reason)]."""
    exits = []
    for sym, days in held_days.items():
        s = signals.get(sym)
        if days is not None and days >= tc["max_hold_days"]:
            exits.append((sym, f"held {days} trading days"))
        elif s and s["score"] <= sc["exit_threshold"]:
            exits.append((sym, f"sentiment flipped ({s['score']:+.2f})"))
    return exits
