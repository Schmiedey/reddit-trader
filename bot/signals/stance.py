"""Who bought what: classify one post's stance toward one ticker.

Sentiment asks "is the wording positive?". This asks "what did the author DO?":
  BUY   bought / loaded / calls / YOLO flair
  HOLD  holding / diamond hands / not selling
  SELL  sold / took profits / puts / short
  DD    bullish write-up with a thesis (DD flair)
  WON   Gain flair (the crowd made money on it)      LOST  Loss flair
Bearish wins/losses (puts, shorts) come back as WON_BEAR / LOST_BEAR so callers can ignore them.
"""
import re

SENTENCES = re.compile(r"(?<=[.!?\n])\s+")
BUY = re.compile(
    r"\b(bought|buying|btfd|buy(?:ing)? the dip|loaded|loading|added|adding|opened|all[- ]in|"
    r"yolo(?:ed|'d|ing)?|going long|i'?m long|long on|picked up|grabbed|scooped|got in|jumped in|"
    r"entered|new position|my position|averag(?:ed|ing) (?:down|up)|dca(?:ing|'d)?|calls?|leaps|"
    r"\d[\d,.]*k?\s*(?:shares|contracts)|shares at|in at \$?\d)\b", re.I)
HOLD = re.compile(
    r"\b(holding|hodl(?:ing)?|not selling|never selling|diamond hands?|still in|long[- ]term hold|"
    r"bag ?hold(?:ing|er)?|sold puts?|selling puts?|csps?|cash[- ]secured)\b", re.I)
SELL = re.compile(
    r"\b(sold|selling|trimmed|trimming|took profits?|taking profits?|cashed out|closed (?:my|out)|"
    r"exited|dumped|dumping|got out|stopped out|bearish)\b", re.I)
BEAR = re.compile(r"\b(puts?|short(?:ing|ed)?|inverse)\b", re.I)
BULL_PUTS = re.compile(r"\b(sold puts?|selling puts?|csps?|cash[- ]secured)\b", re.I)
ASKING = re.compile(r"\b(should i|thinking (?:about|of)|is it (?:too late|time|worth)|worth buying|would you|"
                    r"anyone (?:else )?(?:buying|holding|in)|thoughts on|what do you think)\b", re.I)


def scoped_text(text, ticker, n_tickers):
    """The sentences about this ticker (whole text when it's the only one). Returns (text, direct)."""
    if n_tickers <= 1:
        return text, True
    pat = re.compile(rf"(?<![A-Za-z])\$?{re.escape(ticker)}(?![A-Za-z])")
    hits = [s for s in SENTENCES.split(text) if pat.search(s)]
    return (" ".join(hits), True) if hits else (text, False)


def classify(text, flair="", sentiment=0.0):
    """(stance, strength 0..1) or (None, 0)."""
    f = (flair or "").lower()
    bear = bool(BEAR.search(text)) and not BULL_PUTS.search(text)
    if "gain" in f:
        return ("WON_BEAR" if bear else "WON"), 1.0
    if "loss" in f:
        return ("LOST_BEAR" if bear else "LOST"), 1.0
    if "yolo" in f:
        return ("SELL" if bear else "BUY"), 1.0
    if re.search(r"\bdd\b|due diligence", f):
        if bear or sentiment < -0.05:
            return "SELL", 0.7
        return ("DD", 1.0) if sentiment > 0.05 else (None, 0.0)
    if "?" in text and ASKING.search(text):
        return None, 0.0  # asking whether to buy is not a position
    b, h, s = len(BUY.findall(text)), len(HOLD.findall(text)), len(SELL.findall(text)) + (2 if bear else 0)
    if bear and b:  # "bought puts": the buy word belongs to the bearish bet
        return "SELL", 0.8
    if s > b + h:
        return "SELL", 0.8
    if b and b >= s:
        return "BUY", 0.8
    if h:
        return "HOLD", 0.6
    return None, 0.0


def stance_for(text, ticker, n_tickers, flair="", sentiment=0.0):
    scoped, direct = scoped_text(text, ticker, n_tickers)
    st, strength = classify(scoped, flair, sentiment)
    return st, strength * (1.0 if direct else 0.5)
