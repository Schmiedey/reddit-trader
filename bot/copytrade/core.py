"""Paper copy-trading maths (no I/O). One paper account mirrors one wallet's perp positions.

The account copies the wallet's leverage per coin (position value / wallet equity), capped at
MAX_LEV in total. It only trades a coin when the wallet's own position size in it has changed,
so drift between the two equities doesn't cause fee churn.
"""
START_EQUITY = 10_000.0
FEE = 0.00045        # taker fee per side
SLIP = 0.0005        # assumed slippage per side
MAX_LEV = 5.0        # cap on total |position value| / equity
MIN_CHANGE = 0.005   # wallet size must move by >0.5% to count as a trade


def new_account():
    return {"equity": START_EQUITY, "blown": False, "pos": {}}  # pos: coin -> {size, px, src}


def mark(acct, mids):
    """Mark open positions to current mid prices. Flags the account blown if equity hits zero."""
    for coin, p in acct["pos"].items():
        if coin in mids:
            acct["equity"] += p["size"] * (mids[coin] - p["px"])
            p["px"] = mids[coin]
    if acct["equity"] <= 0:
        acct.update(equity=0.0, blown=True, pos={})
    return acct


def _changed(old, new):
    if old == 0 or new == 0:
        return old != new
    return (old > 0) != (new > 0) or abs(new - old) / abs(old) > MIN_CHANGE


def step(acct, wallet_pos, wallet_equity, mids):
    """Mark to market, then mirror any coin whose wallet position changed.

    wallet_pos: {coin: signed size} or None if the fetch failed (then we only mark).
    Returns a list of trades: (coin, delta_size, price, cost).
    """
    if acct["blown"]:
        return []
    mark(acct, mids)
    if acct["blown"] or wallet_pos is None or wallet_equity <= 0:
        return []
    lev = {c: s * mids[c] / wallet_equity for c, s in wallet_pos.items() if c in mids and s != 0}
    gross = sum(abs(v) for v in lev.values())
    scale = min(1.0, MAX_LEV / gross) if gross > 0 else 1.0
    trades = []
    for coin in set(lev) | set(acct["pos"]):
        if coin not in mids:
            continue
        cur = acct["pos"].get(coin, {"size": 0.0, "src": 0.0})
        src = wallet_pos.get(coin, 0.0)
        if not _changed(cur["src"], src):
            continue
        target = lev.get(coin, 0.0) * scale * acct["equity"] / mids[coin]
        delta = target - cur["size"]
        cost = abs(delta) * mids[coin] * (FEE + SLIP)
        acct["equity"] -= cost
        if target == 0:
            acct["pos"].pop(coin, None)
        else:
            acct["pos"][coin] = {"size": target, "px": mids[coin], "src": src}
        trades.append((coin, delta, mids[coin], cost))
    return trades


def gross_leverage(acct):
    if acct["equity"] <= 0:
        return 0.0
    return sum(abs(p["size"] * p["px"]) for p in acct["pos"].values()) / acct["equity"]
