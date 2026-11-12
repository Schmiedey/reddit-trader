"""Local paper broker: fake cash in SQLite, fills at real market prices from yfinance.

Orders placed today fill at the NEXT trading day's open (no peeking at prices the bot
couldn't have known). Stop-loss and take-profit are checked against each day's high/low;
if both are hit the same day we assume the stop (the conservative choice). A gap past
the stop fills at the open, like a real stop order.
"""


class LocalPaperBroker:
    def __init__(self, con, cfg):
        self.con = con
        self.pc, self.tc = cfg["paper"], cfg["trading"]
        con.execute("INSERT OR IGNORE INTO paper_account VALUES(1, ?)", (self.pc["starting_cash"],))
        con.commit()

    def cash(self):
        return self.con.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()[0]

    def _add_cash(self, amount):
        self.con.execute("UPDATE paper_account SET cash = cash + ? WHERE id=1", (amount,))

    def positions(self):
        return {r["sym"]: r for r in self.con.execute("SELECT * FROM paper_positions")}

    def pending(self):
        return self.con.execute("SELECT * FROM paper_orders WHERE status='pending'").fetchall()

    def queue(self, day, sym, side, qty, reason, stop_pct=None, take_pct=None):
        self.con.execute(
            "INSERT INTO paper_orders(day, sym, side, qty, reason, stop_pct, take_pct) VALUES(?,?,?,?,?,?,?)",
            (day, sym, side, qty, reason, stop_pct, take_pct),
        )

    def _log_exit(self, sym, qty, day, price, reason):
        self.con.execute(
            "INSERT INTO paper_orders(day, sym, side, qty, reason, status, fill_day, fill_price) VALUES(?,?,?,?,?,?,?,?)",
            (day, sym, "SELL", qty, reason, "filled", day, price),
        )

    def _close(self, pos, day, price, reason):
        self._add_cash(pos["qty"] * price)
        self.con.execute("DELETE FROM paper_positions WHERE sym=?", (pos["sym"],))
        self._log_exit(pos["sym"], pos["qty"], day, price, reason)
        return f"SOLD {pos['qty']} {pos['sym']} @ {price:.2f} ({reason})"

    def process(self, bars):
        """Fill pending orders and trigger stops/targets using bars. Returns a list of fill messages."""
        slip = self.pc["slippage_pct"]
        events = []
        for o in self.pending():
            b = next((b for b in bars.get(o["sym"], []) if b["d"] > o["day"]), None)
            if b is None:
                continue
            held = self.positions().get(o["sym"])
            if o["side"] == "BUY":
                price = b["o"] * (1 + slip)
                qty = min(o["qty"], int(self.cash() // price))
                if held or qty < 1:
                    self.con.execute("UPDATE paper_orders SET status='cancelled' WHERE id=?", (o["id"],))
                    continue
                self._add_cash(-qty * price)
                stop_pct = o["stop_pct"] or self.tc["stop_loss_pct"]
                take_pct = o["take_pct"] or self.tc["take_profit_pct"]
                self.con.execute(
                    "INSERT INTO paper_positions VALUES(?,?,?,?,?,?,?)",
                    (o["sym"], qty, price, b["d"], price * (1 - stop_pct), price * (1 + take_pct), None),
                )
                events.append(f"BOUGHT {qty} {o['sym']} @ {price:.2f}")
            else:
                if not held:
                    self.con.execute("UPDATE paper_orders SET status='cancelled' WHERE id=?", (o["id"],))
                    continue
                price = b["o"] * (1 - slip)
                self._add_cash(held["qty"] * price)
                self.con.execute("DELETE FROM paper_positions WHERE sym=?", (o["sym"],))
                events.append(f"SOLD {held['qty']} {o['sym']} @ {price:.2f} ({o['reason']})")
            self.con.execute("UPDATE paper_orders SET status='filled', fill_day=?, fill_price=?, qty=? WHERE id=?",
                             (b["d"], price, qty if o["side"] == "BUY" else held["qty"], o["id"]))

        for pos in self.positions().values():
            last_checked, stop, entry = pos["checked_day"], pos["stop"], pos["entry_price"]
            for b in bars.get(pos["sym"], []):
                if b["d"] < pos["entry_day"] or (last_checked and b["d"] <= last_checked):
                    continue
                gap = b["d"] > pos["entry_day"]  # on entry day we bought at the open, so no gap
                if b["l"] <= stop:
                    why = "stop-loss" if stop < entry else "breakeven stop"
                    events.append(self._close(pos, b["d"], min(b["o"], stop) if gap else stop, why))
                    break
                if b["h"] >= pos["target"]:
                    events.append(self._close(pos, b["d"], max(b["o"], pos["target"]) if gap else pos["target"], "take-profit"))
                    break
                if stop < entry and b["c"] >= 2 * entry - stop:
                    stop = entry  # up by 1x the risk: move the stop to breakeven from tomorrow on
                last_checked = b["d"]
            else:
                self.con.execute("UPDATE paper_positions SET checked_day=?, stop=? WHERE sym=?",
                                 (last_checked, stop, pos["sym"]))
        self.con.commit()
        return events

    def equity(self, bars):
        total = self.cash()
        for p in self.positions().values():
            b = bars.get(p["sym"])
            total += p["qty"] * (b[-1]["c"] if b else p["entry_price"])
        return total
