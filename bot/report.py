"""Static HTML report: data/report.html. No server, no JavaScript. `python -m bot.report` to rebuild."""
import html
import json
import time

from .config import DATA
from .signals.scorecard import scorecard

CSS = """
:root{--bg:#fff;--fg:#1a1a19;--mut:#6b6a66;--line:#e1e0d9;--bot:#2a78d6;--spy:#898781;--up:#0f7a0f;--dn:#c03030}
@media(prefers-color-scheme:dark){:root{--bg:#1a1a19;--fg:#f0efec;--mut:#a3a29c;--line:#2c2c2a;--bot:#3987e5;--up:#4caf50;--dn:#e66767}}
body{font:15px/1.5 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:1100px;margin:2rem auto;padding:0 1rem}
h1{font-size:22px;font-weight:500}h2{font-size:17px;font-weight:500;margin-top:2rem}
.kpis{display:flex;gap:12px;flex-wrap:wrap}.kpi{border:1px solid var(--line);border-radius:10px;padding:.7rem 1rem;min-width:150px}
.kpi b{display:block;font-size:22px;font-weight:500}.kpi span,.mut{color:var(--mut);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500}.up{color:var(--up)}.dn{color:var(--dn)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:0 2rem}
"""


def _table(rows, cols):
    if not rows:
        return '<p class="mut">Nothing yet.</p>'
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _pct(x):
    cls = "up" if x > 0 else "dn" if x < 0 else ""
    return f'<span class="{cls}">{x:+.2f}%</span>'


def _chart(eq):
    """Inline SVG line chart: bot vs SPY, both indexed to 100."""
    pts = [(r["day"], r["equity"], r["spy"]) for r in eq if r["spy"]]
    if len(pts) < 2:
        return '<p class="mut">The chart appears after two daily runs.</p>'
    bot = [e / pts[0][1] * 100 for _, e, _ in pts]
    spy = [s / pts[0][2] * 100 for _, _, s in pts]
    lo, hi = min(bot + spy), max(bot + spy)
    pad = (hi - lo) * 0.1 or 1
    lo, hi = lo - pad, hi + pad
    W, H, L = 680, 220, 40

    def line(vals, color, dash=""):
        xy = " ".join(f"{L + i * (W - L - 10) / (len(vals) - 1):.1f},{H - 20 - (v - lo) / (hi - lo) * (H - 40):.1f}"
                      for i, v in enumerate(vals))
        return f'<polyline points="{xy}" fill="none" stroke="{color}" stroke-width="2" stroke-dasharray="{dash}"/>'

    labels = "".join(
        f'<text x="{L - 6}" y="{H - 20 - (v - lo) / (hi - lo) * (H - 40) + 4:.1f}" text-anchor="end" font-size="11" fill="var(--mut)">{v:.0f}</text>'
        for v in (lo + pad, hi - pad)
    )
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Bot equity vs SPY">{labels}'
            f'{line(spy, "var(--spy)", "5 4")}{line(bot, "var(--bot)")}'
            f'<text x="{L}" y="{H - 2}" font-size="11" fill="var(--mut)">{pts[0][0]}</text>'
            f'<text x="{W - 10}" y="{H - 2}" font-size="11" fill="var(--mut)" text-anchor="end">{pts[-1][0]}</text></svg>'
            '<p class="mut"><span style="color:var(--bot)">━</span> Bot &nbsp; <span style="color:var(--spy)">┅</span> S&amp;P 500 (SPY), start = 100</p>')


def write_report(con, bars=None):
    bars = bars or {}
    eq = con.execute("SELECT * FROM equity ORDER BY day").fetchall()
    kpis = ""
    if eq:
        first, last = eq[0], eq[-1]
        bot_r = (last["equity"] / first["equity"] - 1) * 100
        spy_r = (last["spy"] / first["spy"] - 1) * 100 if first["spy"] and last["spy"] else 0
        kpis = (f'<div class="kpis"><div class="kpi"><span>Paper equity</span><b>${last["equity"]:,.0f}</b></div>'
                f'<div class="kpi"><span>Bot return</span><b>{_pct(bot_r)}</b></div>'
                f'<div class="kpi"><span>SPY return</span><b>{_pct(spy_r)}</b></div>'
                f'<div class="kpi"><span>Bot minus SPY</span><b>{_pct(bot_r - spy_r)}</b></div></div>')

    pos = []
    for p in con.execute("SELECT * FROM paper_positions ORDER BY entry_day"):
        last = (bars.get(p["sym"]) or [{"c": p["entry_price"]}])[-1]["c"]
        pos.append((p["sym"], p["qty"], f'{p["entry_price"]:.2f}', f"{last:.2f}", _pct((last / p["entry_price"] - 1) * 100),
                    p["entry_day"], f'{p["stop"]:.2f} / {p["target"]:.2f}'))

    sig = []
    for r in con.execute("SELECT ticker, score, detail FROM signals WHERE day=(SELECT MAX(day) FROM signals) ORDER BY score DESC LIMIT 25"):
        d = json.loads(r["detail"])
        sig.append((r["ticker"], f'{r["score"]:+.2f}', ", ".join(d["agree"]), d["voices"],
                    f'{d.get("agreement", 0):.0%}', html.escape(d.get("blocked") or d.get("veto") or ""),
                    html.escape("; ".join(d["top_voices"]))))
    card = [(label, n, _pct(avg) if n else "", f"{hit:.0f}%" if n else "") for label, n, avg, hit in scorecard(con)]

    orders = [(o["day"], o["side"], o["sym"], o["qty"], o["status"], f'{o["fill_price"]:.2f}' if o["fill_price"] else "",
               html.escape(o["reason"] or "")) for o in con.execute("SELECT * FROM paper_orders ORDER BY id DESC LIMIT 40")]
    blocked = [(b["day"], b["ticker"], html.escape(b["reason"])) for b in
               con.execute("SELECT * FROM blocked ORDER BY day DESC LIMIT 25")]
    trend = [(r["ticker"], r["src"], r["voices"], f'{r["s"]:+.2f}') for r in con.execute(
        """SELECT ticker, GROUP_CONCAT(DISTINCT source) src, COUNT(DISTINCT source||author) voices, AVG(sentiment) s
           FROM mentions WHERE created>=? GROUP BY ticker ORDER BY voices DESC LIMIT 20""", (time.time() - 86400,))]
    leaders = [(a["source"], html.escape(a["author"]), a["hits"], a["misses"], f'{100 * a["hits"] / (a["hits"] + a["misses"]):.0f}%')
               for a in con.execute("""SELECT * FROM authors WHERE hits+misses>=3
                                       ORDER BY 1.0*(hits+2)/(hits+misses+4) DESC LIMIT 30""")]

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Paper bot report</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head><body>
<h1>Reddit paper-trading bot</h1><p class="mut">Updated {time.strftime('%Y-%m-%d %H:%M')}. Fake money only.</p>
{kpis}{_chart(eq)}
<h2>Open positions</h2>{_table(pos, ["Ticker", "Shares", "Entry", "Last", "P&L", "Since", "Stop / target"])}
<h2>Does the logic work? Signal scorecard</h2>
<p class="mut">Every past signal, graded by the stock's return over the next 10 trading days minus SPY's.
If "Strong buy" doesn't beat "Neutral" after a few weeks, the logic isn't working.</p>
{_table(card, ["Score bucket", "Signals graded", "Avg vs SPY", "Beat SPY"])}
<div class="grid"><div><h2>Latest signals</h2>{_table(sig, ["Ticker", "Score", "Agree", "Voices", "Consensus", "Skipped because", "Top voices"])}</div>
<div><h2>Trending (24h)</h2>{_table(trend, ["Ticker", "Sources", "Voices", "Avg sentiment"])}</div></div>
<h2>Orders</h2>{_table(orders, ["Day", "Side", "Ticker", "Qty", "Status", "Fill", "Why"])}
<div class="grid"><div><h2>Blocked (likely pumps)</h2>{_table(blocked, ["Day", "Ticker", "Reason"])}</div>
<div><h2>Most-right authors</h2>{_table(leaders, ["Source", "Author", "Right", "Wrong", "Hit rate"])}</div></div>
</body></html>"""
    path = DATA / "report.html"
    path.write_text(page)
    return path


if __name__ == "__main__":
    from .db import connect

    print(write_report(connect()))
