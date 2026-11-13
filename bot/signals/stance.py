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


# ---------- comments ----------
# Comments are short, sarcastic and full of chatter about what the PRICE or OTHER PEOPLE are doing, so
# they get stricter rules than posts: only a sentence in which the author states their own position.
C_BEAR = re.compile(
    r"\bputs\b|\b\d+(?:\.\d+)?p\b|\b(?:a|an|my|the|one|this|that|\d+[kp]?)\s+(?:\$?[A-Za-z]{2,5}\s+)?put\b(?!\s+(?:in|on|it|into|up|down|out))|\bput\s+(?:options?|lottos?|contracts?|spreads?)|"
    r"\b(?:i'?m|im|i am|i was|i went|going|went|am|stay(?:ing)?)\s+short\b|\bshort(?:ing|ed)\b|"
    r"\b(?:a|my|the|took|opened)\s+short\b(?![- ](?:squeeze|term|interest|of))|\bshort\s+(?:position|shares)|\binverse\b", re.I)
C_SELL = re.compile(
    r"\b(sold|selling(?! off)|trimmed|trimming|took profits?|taking profits?|cashed out|closed (?:my|out)|exited|"
    r"got out|stopped out|dumped (?:my|it|them|all|everything|half)|i dumped)\b", re.I)
C_BUY = re.compile(
    r"\b(bought|buying|loaded|loading|added|adding|opened|all[- ]in|yolo(?:ed|'d|ing)?|i'?m long|picked up|grabbed|"
    r"scooped|got in|jumped in|new position|averag(?:ed|ing) (?:down|up)|dca(?:ing|'d)?|"
    r"(?<!earnings )(?<!conference )calls|call options?|leaps|\d[\d,.]*k?\s*shares|shares at|in at \$?\d)\b", re.I)
C_BULL_PUTS = re.compile(r"\b(?:sold|selling|sell|wrote|writing)\s+(?:\S+\s+){0,3}puts?\b(?!\s+for)|\bcsps?\b|cash[- ]secured|"
                         r"puts?\s+(?:got\s+)?assigned", re.I)
BOTH_WAYS = re.compile(r"\bcalls?\b.*\bputs?\b|\bputs?\b.*\bcalls?\b", re.I)
CLOSED_BET = re.compile(r"\b(sold|closed|exited|dumped|cashed out)\b[^.!?]{0,25}\b(puts?|calls?|shorts?)\b", re.I)
YOU = re.compile(r"\b(you|your|you'?re|u|ur|y'?all)\b", re.I)
THEM = re.compile(r"\b(he|she|they|them|their|people|everyone|everybody|anyone|anybody|someone|somebody|guys?|bro|"
                  r"institutions?|retail|bulls?|bears?|mms?|algos?|wsb|whoever|asians?|europeans?|boomers?)\b", re.I)
HYPO = re.compile(r"\b(should(?:'?ve| have|a)?|could(?:'?ve| have|a)?|would(?:'?ve| have|a)?|wish|regret\w*|tempted|"
                  r"imagine|what if|if i|might|maybe|thinking|want(?:ed)? to|plan(?:ning)? to|about to|meant to|almost|"
                  r"close to|considering|wondering|do i|should i|talk me out|hindsight|urge)\b", re.I)
NEG = re.compile(r"\b(not|never|didn'?t|don'?t|doesn'?t|won'?t|wouldn'?t|can'?t|couldn'?t|ain'?t|isn'?t|aren'?t|"
                 r"glad i|good thing)\b|n't\b", re.I)


def comment_stance(text, ticker, n_tickers):
    """BUY / SELL (sold, or a bearish bet) / HOLD / None for one comment and one ticker."""
    scoped, _ = scoped_text(text, ticker, n_tickers)
    b = s = bear = h = 0
    for sent in SENTENCES.split(scoped):
        sent = sent.strip()
        if not sent or sent.endswith("?") or YOU.search(sent):
            continue  # a question, or talking to someone else
        mine = bool(FIRST.search(sent))
        if not mine and (THEM.search(sent) or len(sent.split()) > 6):
            continue  # someone else's trade, or commentary: only "I ..." sentences and short lines count
        if HYPO.search(sent):
            continue  # "should have bought", "do I buy", "tempted to"
        if C_BULL_PUTS.search(sent):
            h += 1  # selling puts is a bet the stock holds up
            continue
        if HOLD.search(sent) and not C_BEAR.search(sent):
            h += 1
            continue
        if NEG.search(sent) or (CLOSED_BET.search(sent) and not HOLD.search(sent)):
            continue  # "did not buy", "sold my puts" (a closed bet, no position left)
        if BOTH_WAYS.search(sent) and not mine:
            continue  # "AMD puts or calls"
        if C_BEAR.search(sent):
            bear += 1
        else:
            b += len(C_BUY.findall(sent))
            s += len(C_SELL.findall(sent))
    if bear or s > b:
        return "SELL" if bear + s > b or bear else None
    if b > s:
        return "BUY"
    return "HOLD" if h else None
