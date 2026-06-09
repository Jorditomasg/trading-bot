"""WAL journal mode — set once at schema init, persists per DB file.

WAL allows the dashboard process to read while the bot's threads write
(and vice versa) without `database is locked` errors.
"""

from bot.database.db import Database


def test_journal_mode_is_wal(tmp_path):
    db = Database(path=str(tmp_path / "wal_test.db"))
    with db._conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_journal_mode_survives_reopen(tmp_path):
    """WAL is a persistent file property — a second Database on the same path keeps it."""
    path = str(tmp_path / "wal_test.db")
    Database(path=path)
    db2 = Database(path=path)
    with db2._conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
