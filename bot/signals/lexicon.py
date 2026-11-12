# Extra VADER valences (-4..4) for finance / WSB slang. VADER converts emoji to words
# (e.g. the rocket emoji becomes "rocket"), so emoji are covered by their word here.
WSB_LEXICON = {
    "bullish": 2.5, "bearish": -2.5, "calls": 1.5, "puts": -1.5,
    "moon": 2.0, "mooning": 2.5, "rocket": 2.5, "tendies": 2.0, "lambo": 1.5,
    "squeeze": 1.5, "breakout": 1.5, "rally": 2.0, "rallying": 2.0, "ripping": 2.0,
    "printing": 1.8, "undervalued": 2.0, "overvalued": -2.0, "upgrade": 2.0,
    "upgraded": 2.0, "downgrade": -2.0, "downgraded": -2.0, "beat": 1.2, "beats": 1.2,
    "miss": -1.5, "missed": -1.5, "buyback": 1.5, "outperform": 2.0, "underperform": -2.0,
    "bagholder": -2.0, "bagholding": -2.0, "bagholders": -2.0, "rugpull": -3.0,
    "dump": -2.0, "dumping": -2.0, "dumped": -2.0, "crash": -2.5, "crashing": -2.5,
    "tank": -2.0, "tanking": -2.5, "tanked": -2.5, "drilling": -2.0, "plummet": -2.5,
    "short": -0.8, "shorting": -1.2, "shorted": -1.0, "dilution": -2.0, "offering": -1.0,
    "bankrupt": -3.0, "bankruptcy": -3.0, "delisted": -3.0, "fraud": -3.0, "lawsuit": -1.5,
    "guh": -2.0, "rekt": -2.5, "worthless": -2.5, "chart increasing": 2.0,
    "chart decreasing": -2.0, "bear": -1.5, "bull": 1.5,
}
