"""Turn mentions + news into one score per ticker.

The rules, most important first:
- Credibility: each author counts by how often their past calls beat SPY.
- One opinion per author per ticker; spamming a ticker adds nothing.
- Recency: opinions lose half their weight every 24h.
- Quality: long DD-flaired Reddit posts count more than meme/YOLO posts.
- Consensus: a split crowd is discounted, and a handful of voices is shrunk toward zero.
- Hype penalty: a mention spike driven by untested or low-credibility accounts counts
  AGAINST a stock. Retail attention spikes tend to reverse; trusted spikes don't.
"""
import math
import re
import time
from collections import defaultdict

from ..filter.credibility import author_weight

MEME_FLAIRS = ("meme", "yolo", "shitpost", "loss", "gain", "satire")
DD_FLAIR = re.compile(r"\bdd\b|due diligence")


def _sign(x):
    return (x > 0) - (x < 0)


def post_quality(source, flair, body_len, is_post=True):
    """1.0 = ordinary. DD posts with long-form reasoning up to 2.0 (comments in a DD thread 1.2+);
    meme flairs down to 0.3."""
    if source != "reddit":
        return 1.0
    f = (flair or "").lower()
    dd = (0.5 if is_post else 0.2) if DD_FLAIR.search(f) else 0
    q = 1.0 + dd - (0.4 if any(k in f for k in MEME_FLAIRS) else 0)
    q += min(body_len or 0, 3000) / 3000 * 0.5
    return max(0.3, q)


def cred_level(w):
    """Author weight -> 0..1 (credibility 0.3 or less -> 0, 0.7 or more -> 1, untested 0.45 -> ~0.37)."""
    if w <= 0:
        return 0.0
    return min(1.0, max(0.0, (0.5 * math.sqrt(w) - 0.3) / 0.4))


def consensus(g, k):
    """g = [Σw·s, Σw, Σw·|s|, Σw²] -> (value, agreement, effective voices)."""
    mean = g[0] / g[1]
    agree = abs(g[0]) / g[2] if g[2] else 0.0  # 1 = everyone leans the same way, 0 = split down the middle
    n_eff = g[1] ** 2 / g[3]  # a few heavy voices count as fewer people
    return mean * (0.5 + 0.5 * agree) * n_eff / (n_eff + k), agree, n_eff


def _headline_key(h):
    return re.sub(r"\W+", " ", (h or "").rsplit(" - ", 1)[0].lower()).strip()[:80]


def compute_scores(con, cfg, now=None):
    """Return {ticker: signal dict}. Signals with thin=True lack enough trusted voices to trade."""
    now = now or time.time()
    sc, fc = cfg["scoring"], cfg["filter"]
    pump_subs = {s.lower() for s in cfg["reddit"].get("pump_watch", [])}
    cutoff = now - sc["lookback_hours"] * 3600
    buzz_cut = now - sc["buzz_hours"] * 3600
    half_life = sc.get("recency_half_life_hours", 24) * 3600
    k = sc.get("shrink_voices", 3)
    authors = {(r["source"], r["author"]): r for r in con.execute("SELECT * FROM authors")}
    weights = {}

    def weight(src, a):
        if (src, a) not in weights:
            weights[(src, a)] = author_weight(a, authors.get((src, a)), fc)
        return weights[(src, a)]

    per = defaultdict(lambda: [0.0, 0.0, 0, 0.0, 0.0])  # Σs·decay, Σdecay, best upvotes, best quality, latest decay
    recent = defaultdict(lambda: [0, 0.0, 0, 0])  # buzz window: mentions, Σcred level, pump-sub, low-cred
    wires = {a.lower() for a in cfg.get("x", {}).get("accounts", [])}
    wire_news = []
    first = con.execute("SELECT MIN(created) FROM mentions").fetchone()[0]
    warmup = first is None or now - first < sc.get("buzz_warmup_days", 7) * 86400  # no baseline yet
    for r in con.execute(
        """SELECT m.ticker, m.source, m.author, m.sentiment, m.created, p.score, p.community, p.flair,
                  LENGTH(p.body) blen, p.title != '' is_post, SUBSTR(p.body, 1, 120) snippet
           FROM mentions m JOIN posts p ON p.id = m.post_id WHERE m.created >= ? AND m.created < ?""", (cutoff, now),
    ):
        if r["source"] == "x" and (r["author"] or "").lower() in wires:
            wire_news.append((r["ticker"], r["sentiment"], r["created"], f"@{r['author']}: {r['snippet']}"))
            continue  # headline accounts are news, not crowd opinion
        pump = (r["community"] or "").lower() in pump_subs
        w = weight(r["source"], r["author"])
        if r["created"] >= buzz_cut:
            c = recent[r["ticker"]]
            c[0] += 1
            c[1] += cred_level(w)
            c[2] += pump
            c[3] += cred_level(w) < 0.2  # filtered (new/zero-karma) or proven wrong; untested is ~0.37
        if pump or w <= 0:
            continue
        d = 0.5 ** (max(0.0, now - r["created"]) / half_life)
        p = per[(r["ticker"], r["source"], r["author"])]
        p[0] += r["sentiment"] * d
        p[1] += d
        p[2] = max(p[2], r["score"] or 0)
        p[3] = max(p[3], post_quality(r["source"], r["flair"], r["blen"], bool(r["is_post"])))
        p[4] = max(p[4], d)

    agg = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]))
    voices = defaultdict(list)
    for (t, src, a), (sd, dsum, ups, q, dmax) in per.items():
        s = sd / dsum
        w = weight(src, a) * (1 + math.log1p(max(ups, 0)) / 3) * q * dmax
        g = agg[t][src]
        g[0] += w * s
        g[1] += w
        g[2] += w * abs(s)
        g[3] += w * w
        voices[t].append((w * abs(s), src, a, round(s, 2)))

    news = defaultdict(dict)  # ticker -> headline key -> (sentiment, created, headline); dedupes Yahoo vs Google
    for r in con.execute("SELECT tickers, sentiment, headline, created FROM news WHERE created >= ? AND created < ?", (cutoff, now)):
        for t in r["tickers"].strip(",").split(","):
            news[t].setdefault(_headline_key(r["headline"]), (r["sentiment"], r["created"], r["headline"]))
    for t, s, created, text in wire_news:
        news[t].setdefault(_headline_key(text.split(": ", 1)[-1]), (s, created, text))

    base_days = max(sc["baseline_days"] - sc["buzz_hours"] / 24, 1)
    older = {r[0]: r[1] for r in con.execute(
        "SELECT ticker, COUNT(*) FROM mentions WHERE created >= ? AND created < ? GROUP BY ticker",
        (now - sc["baseline_days"] * 86400, buzz_cut),
    )}

    cw, W = sc["crowd_weights"], sc["weights"]
    out = {}
    for t, srcs in agg.items():
        parts, agreements = {}, {}
        for src, g in srcs.items():
            if g[1] > 0:
                parts[src], agreements[src], _ = consensus(g, k)
        tot = sum(cw.get(s, 0) for s in parts)
        crowd = sum(cw.get(s, 0) * v for s, v in parts.items()) / tot if tot else 0.0
        all_g = [sum(g[i] for g in srcs.values()) for i in range(4)]
        _, crowd_agree, n_eff = consensus(all_g, k) if all_g[1] > 0 else (0, 0, 0)

        items = sorted(news[t].values(), key=lambda x: -x[1])
        if items:
            ds = [0.5 ** (max(0.0, now - c) / half_life) for _, c, _ in items]
            news_v = sum(s * d for (s, _, _), d in zip(items, ds)) / sum(ds) * len(items) / (len(items) + 2)
        else:
            news_v = None

        m24, cred24, pump24, low24 = recent[t]
        expected = older.get(t, 0) / base_days * (sc["buzz_hours"] / 24)
        ratio = 1.0 if warmup else m24 / max(expected, 1)
        buzz = max(0.0, math.tanh((ratio - 1) / 3))
        cred_share = cred24 / m24 if m24 else 0.0
        hype = buzz * (2 * cred_share - 1)  # >0: trusted voices piling in; <0: untested/low-cred crowd
        buzz_term = hype * _sign(crowd) if hype > 0 else hype

        score = W["crowd"] * crowd + W["news"] * (news_v or 0.0) + W["buzz"] * buzz_term
        if news_v is not None:
            parts["news"] = news_v
        agree = [s for s, v in parts.items() if abs(v) >= 0.05 and _sign(v) == _sign(score)]
        out[t] = {
            "ticker": t, "score": round(score, 3), "crowd": round(crowd, 3), "news": round(news_v or 0.0, 3),
            "buzz": round(buzz_term, 3), "parts": {s: round(v, 3) for s, v in parts.items()}, "agree": agree,
            "agreement": round(crowd_agree, 2), "eff_voices": round(n_eff, 1),
            "voices": len(voices[t]), "thin": len(voices[t]) < sc["min_authors"],
            "spike_ratio": round(ratio, 1), "cred_share": round(cred_share, 2),
            "lowcred_share": round(low24 / m24, 2) if m24 else 0.0,
            "pump_sub_share": round(pump24 / m24, 2) if m24 else 0.0,
            "top_voices": [f"{src}:{a} ({s:+.2f})" for _, src, a, s in sorted(voices[t], reverse=True)[:3]],
            "headlines": [h for _, _, h in items[:3]],
        }
    return out
