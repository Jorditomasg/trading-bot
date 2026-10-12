"""Move a running bot from the fixed-TP B-pick to the chandelier exit (gotcha #50).

    PYTHONPATH=. python scripts/migrate_trailing_exit_2026_10.py --db /app/data/trading_bot.db [--dry-run]

Only rewrites a key whose current value is the old B-pick (or missing), so a
deliberate user customisation is left alone. Prints old → new for the record.
Restart the bot afterwards: `_apply_ema_config` reads these at startup.
"""
from __future__ import annotations

import argparse
import os

from bot.database.db import Database

OLD = {"ema_stop_mult": "1.5", "ema_tp_mult": "5.0", "ema_trail_mult": None}
NEW = {"ema_stop_mult": "2.0", "ema_tp_mult": "0.0", "ema_trail_mult": "4.0"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DB_PATH", "trading_bot.db"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = Database(a.db)
    cfg = db.get_runtime_config()
    open_trades = db.get_open_trades()
    if open_trades:
        print(f"NOTE: {len(open_trades)} open trade(s) keep their stored SL/TP; "
              "only the trailing ratchet starts applying to them.")
    changes = {}
    for k, new in NEW.items():
        cur = cfg.get(k)
        if cur == new:
            print(f"  {k}: already {new}")
        elif cur == OLD[k] or cur is None:
            changes[k] = new
            print(f"  {k}: {cur!r} → {new!r}")
        else:
            print(f"  {k}: {cur!r} is a custom value — left alone")
    if changes and not a.dry_run:
        db.set_runtime_config(**changes)
        print("applied — restart the bot to load it")
    elif a.dry_run:
        print("dry-run: nothing written")


if __name__ == "__main__":
    main()
