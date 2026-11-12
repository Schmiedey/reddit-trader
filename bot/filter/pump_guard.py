def check(sig, bars, fc):
    """Return a reason string if this ticker should not be traded, else None."""
    if not bars:
        return "no price data"
    last = bars[-1]["c"]
    if last < fc["min_price"]:
        return f"price ${last:.2f} under ${fc['min_price']}"
    recent = bars[-20:]
    avg_vol = sum(b["v"] for b in recent) / len(recent)
    if avg_vol < fc["min_avg_volume"]:
        return f"thin volume ({avg_vol:,.0f}/day)"
    if sig.get("spike_ratio", 0) >= fc["pump_spike_ratio"] and sig.get("lowcred_share", 0) >= fc["pump_lowcred_share"]:
        return f"{sig['spike_ratio']:.0f}x mention spike, mostly from low-credibility accounts"
    if sig.get("pump_sub_share", 0) >= fc["pump_sub_share"]:
        return "heavily pushed in pump-watch subreddits"
    return None
