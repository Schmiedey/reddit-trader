"""How much "who is buying / selling / holding" signal is in Reddit COMMENTS, compared with posts?

Reads the day files from scripts/backfill_comments.py and classifies each comment with the same stance
rules as posts. A comment's ticker comes from (A) an uppercase or $cashtag ticker in the comment,
(B) a well-known ticker typed in lowercase ("nvda calls"), or (C) the post it replies under, when that
post is about exactly one stock.

    uv run python -m bot.backtest.comments_probe [--sub wallstreetbets] [--sample 100]
"""
import argparse
import gzip
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from ..cloud.engine import FUNDS
from ..config import DATA
from ..filter.tickers import extract_tickers
from ..signals.stance import comment_stance
from ..sources.prices import load_valid_symbols
from .build import bt_connect

# Tickers people type in lowercase that are not also ordinary words.
LOWER = set("""NVDA TSLA AMD PLTR MSFT RKLB MSTR AMZN BYND ASTS RDDT AAPL INTC SMCI UNH GOOGL GOOG NBIS
LUNR ACHR AVGO GME HIMS ASML SNDK SOFI NFLX TSM RGTI OKLO IREN ORCL CRWV CVNA BBAI BABA APLD IONQ NIO SOUN
BMNR QBTS MARA RIVN CRWD PYPL LCID CELH QCOM MRVL LULU MRNA PANW TLRY ADBE""".split())
LOWER_RE = re.compile(r"(?<![A-Za-z$])(" + "|".join(sorted(LOWER)) + r")(?![A-Za-z])", re.I)
# Valid symbols that are really jargon in this crowd's writing.
JARGON = set("RSI HYSA PR IP ET DYOR CC TV NASA UAE EMA GPT WTF NATO PC ESG OI PDT AMA CPU NYC VC DC IQ TIME BTC ETH "
             "XRP SK III SI CD PS BC SMA MA MC TD AG UI SE ES FT AR RS RF GLP HD MAGA RAM GOP DEI EOD CEO USA".split())
DEAD = {None, "", "[deleted]", "[removed]", "AutoModerator", "VisualMod"}


def load(sub):
    for f in sorted((DATA / "history" / "comments_stance" / sub).glob("*.jsonl.gz")):
        with gzip.open(f, "rt") as g:
            for line in g:
                yield f.stem, json.loads(line)


def classify_comment(c, valid, post_tickers):
    """[(tier, ticker, stance)] for one comment; stance may be None."""
    body = c.get("body") or ""
    if c.get("author") in DEAD or body in DEAD:
        return []
    text = LOWER_RE.sub(lambda m: m.group(1).upper(), body)
    a = extract_tickers(body, valid) - JARGON
    b = (extract_tickers(text, valid) - JARGON) - a
    tks, tier = (a | b), {**{t: "B" for t in b}, **{t: "A" for t in a}}
    if not tks:
        parent = post_tickers.get((c.get("link_id") or "")[3:])
        if not parent:
            return []
        tks, tier = {parent}, {parent: "C"}
    if len(tks) > 5:
        return []
    if "GOOG" in tks:  # two share classes of one company
        tier["GOOGL"] = tier.get("GOOGL") or tier["GOOG"]
        tks = (tks - {"GOOG"}) | {"GOOGL"}
    return [(tier[t], t, comment_stance(text, t, len(tks))) for t in tks]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default="wallstreetbets")
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=str(DATA / "backtest" / "comments_sample.txt"))
    a = ap.parse_args()
    con = bt_connect()
    valid = load_valid_symbols(con)
    one = {}
    for pid, tk, n in con.execute("SELECT post_id, MIN(ticker), COUNT(*) FROM mentions WHERE source='reddit' GROUP BY post_id"):
        if n == 1 and tk not in JARGON:
            one[pid.split(":")[-1]] = tk
    days, total, alive = set(), 0, 0
    tiers, stances = Counter(), Counter()
    who = defaultdict(lambda: defaultdict(set))  # ticker -> stance -> authors
    authors, rows = set(), []
    for day, c in load(a.sub):
        days.add(day)
        total += 1
        res = classify_comment(c, valid, one)
        alive += c.get("author") not in DEAD
        for tier, t, st in res:
            tiers[tier] += 1
            if st in ("BUY", "DD", "SELL", "HOLD"):
                st = "BUY" if st == "DD" else st
                stances[(tier, st)] += 1
                who[t][st].add(c["author"])
                authors.add(c["author"])
                rows.append((tier, t, st, " ".join((c.get("body") or "").split())[:170]))
    n = max(len(days), 1)
    print(f"{a.sub}: {len(days)} day(s), {total:,} comments ({total / n:,.0f}/day), {alive:,} with a live author")
    print(f"ticker found: in comment {tiers['A']:,}, lowercase {tiers['B']:,}, from the post it replies to {tiers['C']:,}")
    print(f"{'stance':<6}{'A':>7}{'B':>7}{'C':>7}{'total':>8}{'/day':>8}")
    for st in ("BUY", "SELL", "HOLD"):
        v = [stances[(t, st)] for t in "ABC"]
        print(f"{st:<6}{v[0]:>7}{v[1]:>7}{v[2]:>7}{sum(v):>8}{sum(v) / n:>8.0f}")
    print(f"distinct authors with a stance: {len(authors):,}")
    lo, hi = f"{min(days)} 00:00:00", f"{max(days)} 23:59:59"
    posts = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT ticker, COUNT(DISTINCT CASE WHEN stance IN ('BUY','DD') THEN author END), "
        "COUNT(DISTINCT CASE WHEN stance='SELL' THEN author END) FROM stances WHERE community=? AND "
        "created BETWEEN CAST(strftime('%s', ?) AS REAL) AND CAST(strftime('%s', ?) AS REAL) GROUP BY ticker", (a.sub.lower(), lo, hi))}
    top = sorted((t for t in who if t not in FUNDS), key=lambda t: -sum(len(s) for s in who[t].values()))[:15]
    print(f"\n{'ticker':<7}{'buyers':>7}{'sellers':>8}{'holders':>8}{'majority':>14}{'  posts: buyers / sellers':>26}")
    for t in top:
        b, s, h = (len(who[t][k]) for k in ("BUY", "SELL", "HOLD"))
        side, share = max((("buy", b), ("sell", s), ("hold", h)), key=lambda x: x[1])
        pb, ps = posts.get(t, (0, 0))
        print(f"{t:<7}{b:>7}{s:>8}{h:>8}{side + f' {share / (b + s + h):.0%}':>14}{f'{pb} / {ps}':>26}")
    funds = {t: sum(len(s) for s in who[t].values()) for t in who if t in FUNDS}
    print("funds (excluded from signals):", dict(sorted(funds.items(), key=lambda x: -x[1])[:5]))
    random.Random(a.seed).shuffle(rows)
    Path(a.out).write_text("\n".join(f"{i + 1}|{r[0]}|{r[1]}|{r[2]}| {r[3]}" for i, r in enumerate(rows[:a.sample])))
    print("sample written to", a.out)


if __name__ == "__main__":
    main()
