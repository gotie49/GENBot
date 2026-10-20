import os
import sqlite3
import time

from statustw_bot import backup, healthcheck
from statustw_bot.storage import Storage


async def test_backup_while_open(tmp_path, monkeypatch):
    db = tmp_path / "bot.sqlite3"
    storage = Storage(str(db))
    await storage.open()
    await storage.add_player("Smash")
    target = tmp_path / "backup.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    assert backup.main(["backup", str(target)]) == 0  # bot connection still open (WAL)
    await storage.close()
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT name FROM players").fetchall() == [("Smash",)]


def test_healthcheck(tmp_path, monkeypatch):
    hb = tmp_path / "hb"
    monkeypatch.setenv("HEARTBEAT_FILE", str(hb))
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "60")
    assert healthcheck.main() == 1  # missing
    hb.write_text("1")
    assert healthcheck.main() == 0
    old = time.time() - 3600
    os.utime(hb, (old, old))
    assert healthcheck.main() == 1
