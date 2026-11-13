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
    r"entered|new position|my position|averag(?:ed|ing) (?:down|up)|dca(?:ing|'d)?|(?<!earnings )(?<!conference )calls|"
    r"call options?|\d+c|leaps|"
    r"\d[\d,.]*k?\s*(?:shares|contracts)|shares at|in at \$?\d)\b", re.I)
HOLD = re.compile(
    r"\b(holding|hodl(?:ing)?|not selling|never selling|diamond hands?|still in|long[- ]term hold|"
    r"bag ?hold(?:ing|er)?|sold puts?|selling puts?|csps?|cash[- ]secured)\b", re.I)
SELL = re.compile(
    r"\b(sold|selling|trimmed|trimming|took profits?|taking profits?|cashed out|closed (?:my|out)|"
    r"exited|dumped|dumping|got out|stopped out|bearish)\b", re.I)
BEAR = re.compile(r"\b(puts?|short(?:ing|ed)?|inverse)\b", re.I)
BULL_PUTS = re.compile(r"\b(sold puts?|selling puts?|csps?|cash[- ]secured)\b", re.I)
FIRST = re.compile(r"\b(i|i'?m|im|i'?ve|ive|i'?d|i'?ll|my|me|mine|we|we'?re|we'?ve|our|us)\b", re.I)
INTENT = re.compile(r"\b(thinking (?:to|about|of)|want(?:ing)? to|plan(?:ning)? to|preparing to|considering|"
                    r"might|would|should|could|wondering|if i)\b", re.I)
DONE = re.compile(r"\b(bought|loaded|added|opened|grabbed|picked up|scooped|got in|yolo(?:ed|'d)|in at|own|holding|"
                  r"my (?:position|calls|shares|leaps)|i'?m (?:long|in))\b", re.I)
ASKING = re.compile(r"\b(should i|thinking (?:about|of)|is it (?:too late|time|worth)|worth buying|would you|"
                    r"anyone (?:else )?(?:buying|holding|in)|thoughts on|what do you think)\b", re.I)


def scoped_text(text, ticker, n_tickers):
    """The sentences about this ticker (whole text when it's the only one). Returns (text, direct)."""
    if n_tickers <= 1:
        return text, True
    pat = re.compile(rf"(?<![A-Za-z])\$?{re.escape(ticker)}(?![A-Za-z])")
    hits = [s for s in SENTENCES.split(text) if pat.search(s)]
    return (" ".join(hits), True) if hits else (text, False)


def own_buy_text(text):
    """Only the sentences where the author says THEY bought: first person, or a short title-style line
    ("Bought NVDA calls at open"). Drops questions, plans ("thinking to buy") and other people's trades
    ("Salazar bought NVDA", "$10k bought at Wednesday's close was worth...")."""
    keep = []
    for s in SENTENCES.split(text):
        s = s.strip()
        if not s or s.endswith("?") or INTENT.search(s):
            continue
        if FIRST.search(s) or len(s.split()) <= 8:
            keep.append(s)
    return " ".join(keep)


def classify(text, flair="", sentiment=0.0, full=None, title=""):
    """(stance, strength 0..1) or (None, 0). `full` is the whole post when `text` is only the part about
    one ticker; `title` is used to spot questions."""
    f = (flair or "").lower()
    full = text if full is None else full
    bear = bool(BEAR.search(text)) and not BULL_PUTS.search(text)
    if not bear and BEAR.search(full) and not BULL_PUTS.search(full) and not re.search(r"\bcalls\b", text, re.I):
        bear = True  # "69 PUTS, $115K" in the title, ticker named in another sentence
    question = "?" in (title or "") and not (FIRST.search(full) and DONE.search(own_buy_text(full)))
    if "gain" in f:
        return ("WON_BEAR" if bear else "WON"), 1.0
    if "loss" in f:
        return ("LOST_BEAR" if bear else "LOST"), 1.0
    if question:
        return None, 0.0  # "So... NVDA calls tomorrow?", "Buying opportunity?": asking, not a position
    if "yolo" in f:
        return ("SELL" if bear else "BUY"), 1.0
    if re.search(r"\bdd\b|due diligence", f):
        if bear or sentiment < -0.05:
            return "SELL", 0.7
        return ("DD", 1.0) if sentiment > 0.05 else (None, 0.0)
    if "?" in text and ASKING.search(text):
        return None, 0.0  # asking whether to buy is not a position
    b = len(BUY.findall(own_buy_text(text)))
    h, s = len(HOLD.findall(text)), len(SELL.findall(text)) + (2 if bear else 0)
    if bear and b:  # "bought puts": the buy word belongs to the bearish bet
        return "SELL", 0.8
    if s > b + h:
        return "SELL", 0.8
    if b and b > s:
        return "BUY", 0.8
    if b and s:
        return None, 0.0  # "bought and sold today in panic": a round trip, no position left
    if h:
        return "HOLD", 0.6
    return None, 0.0


def in_text(ticker, text):
    return bool(re.search(rf"(?<![A-Za-z])\$?{re.escape(ticker)}(?![A-Za-z])", text or ""))


def stance_for(text, ticker, n_tickers, flair="", sentiment=0.0, title="", others=()):
    """`others`: the post's other tickers. A post whose title names another ticker but not this one is
    about that other stock ("Broadcom (AVGO) hit a 52 week low" mentioning NVDA in passing)."""
    if title and n_tickers > 1 and not in_text(ticker, title) and any(in_text(o, title) for o in others if o != ticker):
        return None, 0.0
    scoped, direct = scoped_text(text, ticker, n_tickers)
    st, strength = classify(scoped, flair, sentiment, full=text, title=title)
    return st, strength * (1.0 if direct else 0.5)
