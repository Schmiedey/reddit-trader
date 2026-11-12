"""How a ticker's sentiment score turns into a buy ranking. Shared by the live bot and the backtest.

- momentum:   buy what the crowd is bullish on (the original idea).
- contrarian: buy what the crowd is bearish on. Backtest Jul-Nov 2026 (Reddit history, see
              BACKTEST.md): bullish crowd sentiment was followed by UNDERperformance, and fading it
              was the only family that held up out of sample.
- attention:  ignore the words, buy names people suddenly talk about more (tested, did not hold up).

weights optionally recombine the parts: score = crowd*w_crowd + buzz*w_buzz + news*w_news.
"""
import math

ALIASES = {"score": "momentum", "inverse": "contrarian"}


def apply_strategy(s, weights=None, mode="momentum"):
    mode = ALIASES.get(mode, mode)
    s = dict(s)
    if weights:
        s["score"] = weights.get("crowd", 0.5) * s["crowd"] + weights.get("buzz", 0.2) * s["buzz"] \
            + weights.get("news", 0.3) * s["news"]
    if mode == "attention":
        s["score"] = 0.5 * math.tanh((s.get("spike_ratio", 1) - 1) / 3)
        s["agree"] = ["reddit"] if s["score"] > 0 else []
        return s
    if mode == "contrarian":
        s["score"] = -s["score"]
    if weights or mode == "contrarian":
        sign = (s["score"] > 0) - (s["score"] < 0)
        flip = -1 if mode == "contrarian" else 1
        s["agree"] = [k for k, v in s["parts"].items() if abs(v) >= 0.05 and ((flip * v > 0) - (flip * v < 0)) == sign]
    if "score" in s:
        s["score"] = round(s["score"], 3)
    return s
