"""Aliases: several in-game names for one watched person."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from statustw_bot.board import player_line
from statustw_bot.commands import MAX_ALIASES, InputError, parse_aliases
from statustw_bot.config import PollStrategy
from statustw_bot.monitor import MonitorOptions, PresenceMonitor
from statustw_bot.notifier import LinkTemplates, Notification, build_embed
from statustw_bot.presence import PresenceState, Status
from statustw_bot.statustw import StatusTwUnavailable
from statustw_bot.storage import MIGRATIONS, NameConflict, Storage
from test_monitor import GER3, OTHER, Clock, FakeApi

LINKS = LinkTemplates(
    player_profile="https://status.tw/players/{name}",
    server="https://status.tw/servers/{ip}/{port}",
    connect="https://gotie49.github.io/GENBot/connect/#{ip}:{port}",
)


# ---------------------------------------------------------------- input parsing


def test_parse_aliases():
    assert parse_aliases(" G0tie, gotie49 ,G0tie,, ", "Gotie") == ["G0tie", "gotie49"]
    assert parse_aliases(None, "Gotie") == [] and parse_aliases("-", "Gotie") == [] and parse_aliases(" ", "Gotie") == []
    with pytest.raises(InputError, match="main name"):
        parse_aliases("Gotie", "Gotie")
    with pytest.raises(InputError, match="at most"):
        parse_aliases(",".join(f"a{i}" for i in range(MAX_ALIASES + 1)), "Gotie")
    with pytest.raises(InputError, match="Alias"):
        parse_aliases("ok, " + "x" * 40, "Gotie")


# ---------------------------------------------------------------- storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(str(tmp_path / "bot.sqlite3"))
    await s.open()
    yield s
    await s.close()


async def test_aliases_roundtrip_and_lookup(storage):
    gotie = await storage.add_player("Gotie")
    await storage.add_player("KOF")
    await storage.set_aliases(gotie.id, ["G0tie", "gotie49"])
    rec = await storage.get_player("Gotie")
    assert rec.aliases == ("G0tie", "gotie49") and rec.names == ("Gotie", "G0tie", "gotie49")
    assert {p.name: p.aliases for p in await storage.list_players()} == {"Gotie": ("G0tie", "gotie49"), "KOF": ()}
    assert await storage.find_owner("gotie49") == "Gotie" and await storage.find_owner("KOF") == "KOF"
    assert await storage.find_owner("nobody") is None

    await storage.set_aliases(gotie.id, ["xGotie"])  # replace
    assert (await storage.get_player("Gotie")).aliases == ("xGotie",)
    await storage.set_aliases(gotie.id, [])  # clear
    assert (await storage.get_player("Gotie")).aliases == ()


async def test_names_are_unique_across_people(storage):
    gotie = await storage.add_player("Gotie")
    kof = await storage.add_player("KOF")
    await storage.set_aliases(gotie.id, ["G0tie"])
    with pytest.raises(NameConflict) as exc:
        await storage.set_aliases(kof.id, ["G0tie"])  # alias of someone else
    assert exc.value.owner == "Gotie"
    with pytest.raises(NameConflict):
        await storage.set_aliases(kof.id, ["Gotie"])  # main name of someone else
    with pytest.raises(NameConflict):
        await storage.add_player("G0tie")  # new person named like an existing alias


async def test_remove_deletes_aliases(storage):
    gotie = await storage.add_player("Gotie")
    await storage.set_aliases(gotie.id, ["G0tie"])
    await storage.remove_player("Gotie")
    assert await storage.find_owner("G0tie") is None
    await storage.add_player("G0tie")  # name is free again


async def test_last_seen_name_persists(storage):
    rec = await storage.add_player("Gotie")
    await storage.save_state(rec.id, PresenceState(status=Status.ONLINE, last_seen_name="G0tie"))
    assert (await storage.get_player("Gotie")).state.last_seen_name == "G0tie"


async def test_migration_from_v2_keeps_players(tmp_path):
    path = tmp_path / "v2.sqlite3"
    with sqlite3.connect(path) as conn:  # database as created by the previous release
        conn.executescript(MIGRATIONS[0] + MIGRATIONS[1] + "PRAGMA user_version = 2;")
        conn.execute("INSERT INTO players (name, created_at) VALUES ('Smash', '2026-10-20T00:00:00+00:00')")
    s = Storage(str(path))
    await s.open()
    [rec] = await s.list_players()
    assert rec.name == "Smash" and rec.aliases == () and rec.state.last_seen_name is None
    await s.close()


# ---------------------------------------------------------------- monitor


class AliasApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.failing_names: set[str] = set()

    async def get_player(self, name):
        if name in self.failing_names:
            self.calls += 1
            raise StatusTwUnavailable("down")
        return await super().get_player(name)


@pytest.fixture
async def env(storage):
    api = AliasApi()
    sent = []

    async def notify(n):
        sent.append(n)

    def make_monitor(**opts):
        return PresenceMonitor(storage, api, notify, MonitorOptions(**opts), clock=Clock())  # type: ignore[arg-type]

    gotie = await storage.add_player("Gotie")
    await storage.set_aliases(gotie.id, ["G0tie", "gotie49"])
    return storage, api, sent, make_monitor


async def test_alias_online_notifies_once_with_seen_name(env):
    storage, api, sent, make_monitor = env
    monitor = make_monitor()
    await monitor.run_cycle()  # baseline: all names offline
    assert api.calls == 3  # one request per name

    api.online["G0tie"] = [GER3]
    await monitor.run_cycle()
    assert len(sent) == 1
    assert sent[0].player_name == "Gotie" and sent[0].seen_name == "G0tie" and sent[0].server_name == GER3.name
    rec = await storage.get_player("Gotie")
    assert rec.state.status is Status.ONLINE and rec.state.last_seen_name == "G0tie"


async def test_switching_alias_while_online_does_not_renotify(env):
    _, api, sent, make_monitor = env
    monitor = make_monitor()
    await monitor.run_cycle()
    api.online["G0tie"] = [GER3]
    await monitor.run_cycle()
    api.online.pop("G0tie")
    api.online["gotie49"] = [OTHER]  # changed name, still online
    for _ in range(3):
        await monitor.run_cycle()
    assert len(sent) == 1


async def test_failed_alias_lookup_is_unknown_not_offline(env):
    storage, api, sent, make_monitor = env
    monitor = make_monitor()
    api.online["Gotie"] = [GER3]
    await monitor.run_cycle()  # online (baseline)
    api.online.clear()
    api.failing_names.add("gotie49")  # two names offline, one unknown
    for _ in range(3):
        r = await monitor.run_cycle()
        assert r.errors == 1
    assert (await storage.get_player("Gotie")).state.status is Status.ONLINE


async def test_online_alias_wins_over_failed_lookup(env):
    storage, api, sent, make_monitor = env
    monitor = make_monitor()
    await monitor.run_cycle()
    api.failing_names.add("Gotie")
    api.online["gotie49"] = [GER3]
    await monitor.run_cycle()
    assert len(sent) == 1 and sent[0].seen_name == "gotie49"


async def test_dummy_with_alias_on_same_server_counts_once(env):
    _, api, sent, make_monitor = env
    monitor = make_monitor()
    await monitor.run_cycle()
    api.online["Gotie"] = [GER3]
    api.online["G0tie"] = [GER3]  # dummy
    await monitor.run_cycle()
    assert len(sent) == 1
    assert sent[0].seen_name == "Gotie"  # main name preferred
    assert sent[0].other_servers == 0  # same server is not "another server"


async def test_bulk_mode_checks_aliases(env):
    _, api, sent, make_monitor = env
    monitor = make_monitor(poll_strategy=PollStrategy.BULK)
    await monitor.run_cycle()
    api.online["gotie49"] = [GER3]
    await monitor.run_cycle()
    assert api.bulk_calls == 2 and api.calls == 0
    assert [n.seen_name for n in sent] == ["gotie49"]


def test_auto_strategy_counts_names(env):
    _, api, _, make_monitor = env
    monitor = make_monitor(poll_interval_seconds=60)
    assert monitor.choose_strategy(60) is PollStrategy.PER_PLAYER
    assert monitor.choose_strategy(61) is PollStrategy.BULK


# ---------------------------------------------------------------- rendering


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _notification(**kw):
    return Notification(player_name="Gotie", detected_at=NOW, server_name="S", server_ip="1.2.3.4", server_port=1, **kw)


def test_notification_shows_alias_and_links_its_profile():
    d = build_embed(_notification(seen_name="G0tie"), LINKS, 0).description
    assert d.startswith("Player [Gotie](https://status.tw/players/G0tie) (as G0tie) connected to")
    plain = build_embed(_notification(seen_name="Gotie"), LINKS, 0).description
    assert plain.startswith("Player [Gotie](https://status.tw/players/Gotie) connected to")
    escaped = build_embed(_notification(seen_name="g_o*t"), LINKS, 0).description
    assert "(as g\\_o\\*t)" in escaped


def _record(status, seen, ago=timedelta(0)):
    from statustw_bot.storage import PlayerRecord

    return PlayerRecord(
        id=1, name="Gotie", enabled=True, profile_url=None, server_filter=None, added_by=None, created_at=NOW,
        aliases=("G0tie",),
        state=PresenceState(status=status, last_online_at=NOW - ago, last_seen_name=seen,
                            last_server_name="S", last_server_address="1.2.3.4:8303"),
    )


def test_board_shows_alias_only_while_online_under_it():
    assert player_line(_record(Status.ONLINE, "G0tie"), LINKS, NOW).startswith("🟢 **Gotie** (G0tie)   [S]")
    assert player_line(_record(Status.ONLINE, "Gotie"), LINKS, NOW).startswith("🟢 **Gotie**   [S]")
    assert player_line(_record(Status.OFFLINE, "G0tie", timedelta(hours=2)), LINKS, NOW).startswith("🟠 **Gotie**   [S]")
