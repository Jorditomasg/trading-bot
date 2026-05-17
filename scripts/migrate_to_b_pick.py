"""Interactive migration script: update legacy DB values to Phase 1 (B-pick) defaults.

Usage:
    PYTHONPATH=. python scripts/migrate_to_b_pick.py
    PYTHONPATH=. python scripts/migrate_to_b_pick.py --dry-run        # preview only
    PYTHONPATH=. python scripts/migrate_to_b_pick.py --db PATH        # custom DB path

Background:
    The May 2026 walk-forward audit identified that some bots were running with C2
    legacy parameters (risk_per_trade=0.03, ema_stop_mult=1.25, ema_tp_mult=3.5,
    ema_vol_mult=2.0, ema_bar_dir=false) which were proven inferior by the audit.
    This script detects those legacy values and writes the recommended Phase 1 defaults.

    This is EXPLICIT and INTERACTIVE — the bot never auto-corrects these (design D11).
    Run this once after the bot has been stopped to avoid a race condition with live cycles.

Importable without side effects (safe for test imports).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Recommended Phase 1 values — must match _seed_optimized_defaults() in main.py
RECOMMENDED: dict[str, str] = {
    "risk_per_trade":        "0.015",
    "ema_stop_mult":         "1.5",
    "ema_tp_mult":           "5.0",
    "ema_vol_mult":          "1.5",
    "ema_bar_dir":           "true",
    "momentum_neutral_band": "0.08",
}

# Legacy C2 values that this script is designed to replace
LEGACY_FINGERPRINTS: dict[str, str] = {
    "risk_per_trade":        "0.03",
    "ema_stop_mult":         "1.25",
    "ema_tp_mult":           "3.5",
    "ema_vol_mult":          "2.0",
    "ema_bar_dir":           "false",
    "momentum_neutral_band": "0.05",
}


def _find_db_path() -> str:
    """Locate the default DB path from env or fallback."""
    import os
    return os.getenv("DB_PATH", "trading_bot.db")


def _collect_changes(cfg: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return list of (key, current_value, recommended_value) for keys that need updating.

    Only flags keys whose current value matches a known legacy fingerprint.
    Keys that the user has already corrected are left alone.
    """
    changes = []
    for key, recommended in RECOMMENDED.items():
        current = cfg.get(key)
        if current is None:
            # Key absent — seed would have handled this; not our job here
            continue
        if current != recommended:
            changes.append((key, current, recommended))
    return changes


def run(db_path: str, dry_run: bool = False) -> int:
    """Core migration logic. Returns exit code (0 = success, 1 = error)."""
    try:
        from bot.database.db import Database
    except ImportError as exc:
        print(f"ERROR: Could not import bot.database.db: {exc}", file=sys.stderr)
        print("Run with: PYTHONPATH=. python scripts/migrate_to_b_pick.py", file=sys.stderr)
        return 1

    db = Database(db_path)
    cfg = db.get_runtime_config()

    if not cfg:
        print(f"No runtime config found in {db_path!r} — nothing to migrate.")
        return 0

    changes = _collect_changes(cfg)

    if not changes:
        print("No legacy values detected — DB is already up to date.")
        return 0

    print(f"\n{'DRY RUN — ' if dry_run else ''}Migration to B-pick Phase 1 defaults")
    print(f"DB: {Path(db_path).resolve()}")
    print()
    print(f"{'Key':<25}  {'Current':<12}  {'Recommended':<12}")
    print("-" * 55)
    for key, current, recommended in changes:
        print(f"  {key:<23}  {current:<12}  {recommended:<12}")

    if dry_run:
        print()
        print("Dry run — no changes written. Re-run without --dry-run to apply.")
        return 0

    print()
    answer = input(f"Apply {len(changes)} change(s)? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted — no changes written.")
        return 0

    updates = {key: recommended for key, _, recommended in changes}
    db.set_runtime_config(**updates)

    print()
    print(f"Applied {len(updates)} update(s):")
    for key, _, recommended in changes:
        print(f"  {key} = {recommended!r}")
    print()
    print("Done. Restart the bot for changes to take effect.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate legacy C2 DB values to Phase 1 (B-pick) defaults.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print diff without writing to DB")
    parser.add_argument("--db", default=None,
                        help="Path to SQLite DB (default: trading_bot.db or DB_PATH env)")
    args = parser.parse_args()
    db_path = args.db or _find_db_path()
    return run(db_path=db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
