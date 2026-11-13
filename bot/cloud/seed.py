"""One-time: build the first ledger so the cloud service starts warm instead of waiting two weeks.

Copies the last weeks of classified Reddit posts from the local database, tops them up from the
archive (the same collector the service uses), runs today's evening job for the three new $100,000
paper accounts, and writes ledger.json for the paper-ledger branch.

    uv run python -m bot.cloud.seed
"""
import json
import shutil
import time

from ..config import DATA
from ..db import connect
from ..signals.crowd import ensure_stances
from .engine import KEEP_DAYS, SUBS, Engine

HOME = DATA / "cloud_seed"


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    eng = Engine(HOME)
    now = time.time()
    src = connect(timeout=900)  # the local collector may hold the database for minutes
    ensure_stances(src)
    subs = {s.lower() for s in SUBS}
    rows = [tuple(r) for r in src.execute("SELECT * FROM stances WHERE created > ?", (now - KEEP_DAYS * 86400,)) if r["community"] in subs]
    eng.con.executemany("INSERT OR IGNORE INTO stances VALUES(?,?,?,?,?,?)", rows)
    eng.con.commit()
    for sub in SUBS:
        last = eng.con.execute("SELECT MAX(created) FROM stances WHERE community=?", (sub.lower(),)).fetchone()[0]
        if last:
            eng.set(f"seen:{sub}", last)
    print(f"copied {len(rows)} classified mentions from the local database")
    res = eng.collect(now, overlap_h=48)
    print("collector:", {k: res[k] for k in ("new", "failed")})
    res = eng.daily(now)
    print("evening run:", json.dumps(res)[:600])
    (HOME / "ledger.json").write_text(json.dumps(eng.export(), separators=(",", ":")))
    print("wrote", HOME / "ledger.json", f"{(HOME / 'ledger.json').stat().st_size / 1e3:.0f} KB")


if __name__ == "__main__":
    main()
