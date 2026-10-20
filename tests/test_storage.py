from datetime import datetime, timezone

import pytest

from statustw_bot.presence import PresenceState, Status
from statustw_bot.storage import MIGRATIONS, PlayerAlreadyExists, Storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(str(tmp_path / "sub" / "bot.sqlite3"))
    await s.open()
    yield s
    await s.close()


async def test_schema_is_created_and_versioned(storage):
    async with storage.db.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == len(MIGRATIONS)


async def test_add_list_remove(storage):
    rec = await storage.add_player("Smash", added_by=42, server_filter="1.2.3.4:8303")
    assert rec.enabled and rec.state.status is Status.UNKNOWN and rec.server_filter == "1.2.3.4:8303"
    with pytest.raises(PlayerAlreadyExists):
        await storage.add_player("Smash")
    # Names are case-sensitive in Teeworlds.
    await storage.add_player("smash")
    assert sorted(p.name for p in await storage.list_players()) == ["Smash", "smash"]
    assert await storage.remove_player("Smash")
    assert not await storage.remove_player("Smash")


async def test_state_survives_reopen(tmp_path):
    path = str(tmp_path / "bot.sqlite3")
    s = Storage(path)
    await s.open()
    rec = await s.add_player("Smash")
    now = datetime(2026, 8, 1, 1, 30, tzinfo=timezone.utc)
    state = PresenceState(status=Status.ONLINE, status_changed_at=now, last_online_at=now,
                          last_server_name="GER3", session_announced=True, last_notified_at=now)
    assert await s.save_state(rec.id, state)
    await s.close()

    s2 = Storage(path)
    await s2.open()  # migrations are idempotent
    loaded = await s2.get_player("Smash")
    await s2.close()
    assert loaded is not None
    assert loaded.state == state


async def test_save_state_ignored_for_disabled_or_removed(storage):
    rec = await storage.add_player("Smash")
    await storage.set_enabled("Smash", False)
    assert not await storage.save_state(rec.id, PresenceState(status=Status.ONLINE))
    await storage.remove_player("Smash")
    assert not await storage.save_state(rec.id, PresenceState(status=Status.ONLINE))


async def test_enable_resets_presence_state(storage):
    rec = await storage.add_player("Smash")
    await storage.save_state(rec.id, PresenceState(status=Status.ONLINE, session_announced=True, offline_streak=1))
    await storage.set_enabled("Smash", False)
    again = await storage.set_enabled("Smash", True)
    assert again is not None and again.enabled
    assert again.state.status is Status.UNKNOWN and not again.state.session_announced


async def test_search_names_escapes_wildcards(storage):
    await storage.add_player("a_b")
    await storage.add_player("axb")
    assert await storage.search_names("_") == ["a_b"]


async def test_board_messages_roundtrip(storage):
    assert await storage.get_board_messages(1) == []
    await storage.set_board_messages(1, [11, 12])
    assert await storage.get_board_messages(1) == [11, 12]
    await storage.set_board_messages(2, [21])  # channel changed in config: old rows dropped
    assert await storage.get_board_messages(1) == [] and await storage.get_board_messages(2) == [21]


async def test_migration_from_v1(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:  # database as created by version 1.0.0
        conn.executescript(MIGRATIONS[0] + "PRAGMA user_version = 1;")
        conn.execute("INSERT INTO players (name, created_at) VALUES ('Smash', '2026-10-20T00:00:00+00:00')")
    s = Storage(str(path))
    await s.open()
    assert [p.name for p in await s.list_players()] == ["Smash"]
    assert await s.get_board_messages(1) == []
    await s.close()
