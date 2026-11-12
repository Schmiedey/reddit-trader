"""Collector entrypoint. launchd runs this every 15 minutes: python -m bot.collect"""
import argparse
import time

from .browser import browser_session
from .config import CFG
from .db import connect
from .sources import news, reddit, stocktwits, x_source
from .sources.prices import load_valid_symbols
from .util import log, lower_priority

# Reddit first: the others only look up tickers that are trending on Reddit.
STEPS = {"reddit": reddit.collect, "stocktwits": stocktwits.collect, "x": x_source.collect, "news": news.collect}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(STEPS), action="append")
    args = ap.parse_args(argv)
    lower_priority()
    con = connect()
    valid = load_valid_symbols(con)
    t0 = time.time()
    with browser_session():
        for name, fn in STEPS.items():
            if args.only and name not in args.only:
                continue
            try:
                log(f"{name}: {fn(con, CFG, valid)} new mentions/items")
            except Exception as e:
                log(f"{name} failed: {type(e).__name__}: {e}")
    log(f"collect done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
