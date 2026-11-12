from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from .lexicon import WSB_LEXICON

_an = None
TAG_VALUE = {"Bullish": 0.8, "Bearish": -0.8}


def analyzer():
    global _an
    if _an is None:
        _an = SentimentIntensityAnalyzer()
        _an.lexicon.update(WSB_LEXICON)
    return _an


def score_text(text):
    """VADER compound score in [-1, 1]. Microseconds per post, no ML model."""
    if not text:
        return 0.0
    return analyzer().polarity_scores(text[:5000])["compound"]


def blend_tag(sent, tag):
    """StockTwits users tag posts Bullish/Bearish; trust the tag more than the text."""
    if tag in TAG_VALUE:
        return 0.6 * TAG_VALUE[tag] + 0.4 * sent
    return sent
