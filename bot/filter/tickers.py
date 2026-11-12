import re

CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")
WORD = re.compile(r"\b([A-Z]{2,5})\b")

# Real tickers that are also common words / finance slang. Only accepted as $CASHTAGS.
BLOCK = set("""
A I AM AN AS AT BE BY DO GO HE IF IN IS IT ME MY NO OF OK ON OR SO TO UP US WE
ALL AND ANY ARE BIG BUT CAN DAY FOR FUN GET HAS HER HIM HIS HOW LOW MAN NEW NOT NOW
ONE OUT OWN PAY RUN SEE SHE THE TOP TWO WAY WHO WHY YES YOU
BEST CARE CASH EVER FAST GAIN GOOD HIGH HOLD HOME JUST KEY LIFE LIKE LOSS LOVE MOON
NEXT NICE OPEN PLAY PLUS REAL ROCK SAFE TRUE VERY WELL WAIT SAVE LOOK MOVE HUGE EDIT
DD YOLO CEO CFO CTO COO IPO ETF ETFS USA UK EU GDP CPI PPI FED FOMC SEC IMO IMHO LOL LMAO
WSB ATH ATL EPS PE EV AI TA FA OTM ITM ATM IV DTE EOD EOW AH PM TLDR FYI OP NYSE NFA
HODL FOMO RIP TOS API FDA IRS DOJ FTC CNBC ETA PT PSA BUY SELL PUT PUTS CALL CALLS
YTD QOQ YOY ROI ROE FCF EBIT USD EUR CAD GBP JPY CNY OTC LLC INC CEOS SPAC REIT
""".split())


def extract_tickers(text, valid):
    """Return the set of valid tickers mentioned in text.

    $CASHTAGS are always accepted if valid. Bare uppercase words are accepted only if
    they are valid, not blocklisted, and the text isn't mostly shouting in caps.
    """
    if not text:
        return set()
    found = {m.upper() for m in CASHTAG.findall(text) if m.upper() in valid}
    letters = [c for c in text if c.isalpha()]
    shouting = letters and sum(c.isupper() for c in letters) / len(letters) > 0.6
    if not shouting:
        found |= {m for m in WORD.findall(text) if m in valid and m not in BLOCK}
    return found
