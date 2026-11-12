"""Keyless market data: ticker list from nasdaqtrader.com, daily bars from yfinance."""
from ..db import cache_get, cache_put, cached_get_text
from ..util import chunks, log

SYMBOL_FILES = (
    ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol"),
    ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol"),
)


def parse_symbol_file(text, sym_col):
    lines = text.splitlines()
    hdr = lines[0].split("|")
    si, ti = hdr.index(sym_col), hdr.index("Test Issue")
    out = set()
    for ln in lines[1:]:
        p = ln.split("|")
        if len(p) <= max(si, ti) or ln.startswith("File Creation"):
            continue
        if p[ti] != "Y" and p[si].isalpha():
            out.add(p[si])
    return out


def load_valid_symbols(con):
    """All US-listed stocks and ETFs (NASDAQ, NYSE, AMEX, ARCA, BATS), refreshed daily."""
    out = set()
    for url, col in SYMBOL_FILES:
        out |= parse_symbol_file(cached_get_text(con, url, ttl=86400), col)
    return out


def _frame_for(df, sym):
    import pandas as pd

    if not isinstance(df.columns, pd.MultiIndex):
        return df
    for level in (0, 1):
        if sym in df.columns.get_level_values(level):
            return df.xs(sym, axis=1, level=level)
    return None


def daily_bars(con, symbols, start):
    """Daily OHLCV since `start` (date). {sym: [{"d","o","h","l","c","v"}]} oldest first. Cached 6h."""
    syms = sorted(set(symbols))
    key = f"yf:{','.join(syms)}:{start}"
    hit = cache_get(con, key, ttl=6 * 3600)
    if hit is not None:
        return hit
    import yfinance as yf

    out = {}
    for chunk in chunks(syms, 50):
        try:
            df = yf.download(chunk, start=start.isoformat(), interval="1d", auto_adjust=True,
                             progress=False, threads=False, group_by="ticker")
        except Exception as e:
            log(f"yfinance failed for {len(chunk)} symbols: {type(e).__name__}")
            continue
        for s in chunk:
            f = _frame_for(df, s)
            if f is None or "Close" not in f:
                continue
            f = f.dropna(subset=["Close"])
            out[s] = [
                {"d": idx.strftime("%Y-%m-%d"), "o": float(r.Open), "h": float(r.High),
                 "l": float(r.Low), "c": float(r.Close), "v": float(r.Volume)}
                for idx, r in f.iterrows()
            ]
    cache_put(con, key, out)
    return out
