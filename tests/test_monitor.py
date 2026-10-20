"""Monitor + real SQLite storage + fake status.tw client."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from statustw_bot.config import PollStrategy, StartupStrategy
from statustw_bot.monitor import MonitorOptions, PresenceMonitor
from statustw_bot.presence import Event, Status
from statustw_bot.statustw import (
    Connection,
    PlayerNotFound,
    PlayerPresence,
    RequestBudget,
    ServerInfo,
    StatusTwUnavailable,
)
from statustw_bot.storage import Storage

GER3 = ServerInfo(ip="116.203.164.237", port=8303, name="[A] [KoG] GER3 #7 - Solo Gores [kog.tw]")
OTHER = ServerInfo(ip="5.39.10.247", port=8303, name="ARX3000 Turkey - Novice")


class FakeApi:
    def __init__(self) -> None:
        self.budget = RequestBudget(60)
        self.online: dict[str, list[ServerInfo]] = {}
        self.unknown: set[str] = set()  # names status.tw never saw (404)
        self.failing = False
        self.calls = 0
        self.bulk_calls = 0
        self.delay = 0.0

    def _presence(self, name: str) -> PlayerPresence:
        servers = self.online.get(name, [])
        return PlayerPresence(name, tuple(Connection(name, s, True, False) for s in servers))

    async def get_player(self, name: str) -> PlayerPresence:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failing:
            raise StatusTwUnavailable("down")
        if name in self.unknown:
            raise PlayerNotFound(name)
        return self._presence(name)

    async def list_online_players(self) -> dict[str, PlayerPresence]:
        self.bulk_calls += 1
        if self.failing:
            raise StatusTwUnavailable("down")
        return {n: self._presence(n) for n, servers in self.online.items() if servers}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 1, 1, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=60)
        return self.now


@pytest.fixture
async def env(tmp_path):
    storage = Storage(str(tmp_path / "bot.sqlite3"))
    await storage.open()
    api = FakeApi()
    sent = []

    async def notify(n):
        sent.append(n)

    def make_monitor(**opts):
        options = MonitorOptions(**{"offline_confirmations": 2, **opts})
        return PresenceMonitor(storage, api, notify, options, clock=Clock())  # type: ignore[arg-type]

    yield storage, api, sent, make_monitor
    await storage.close()


async def test_baseline_then_single_notification_per_session(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    monitor = make_monitor()

    api.online["Smash"] = [GER3]
    await monitor.run_cycle()  # startup baseline: already online -> silent
    assert sent == []

    await storage.get_player("Smash")
    api.online["Smash"] = []
    await monitor.run_cycle()
    r = await monitor.run_cycle()  # second offline observation confirms offline
    assert r.events == [("Smash", Event.WENT_OFFLINE)]
    assert sent == []

    api.online["Smash"] = [GER3]
    for _ in range(5):
        await monitor.run_cycle()
    assert len(sent) == 1
    n = sent[0]
    assert n.player_name == "Smash" and n.server_name == GER3.name
    assert (n.server_ip, n.server_port) == (GER3.ip, GER3.port)


async def test_api_outage_does_not_cause_offline_or_duplicate(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    monitor = make_monitor()
    await monitor.run_cycle()  # offline baseline
    api.online["Smash"] = [GER3]
    await monitor.run_cycle()
    assert len(sent) == 1

    api.failing = True
    for _ in range(5):
        r = await monitor.run_cycle()
        assert r.errors == 1
    rec = await storage.get_player("Smash")
    assert rec.state.status is Status.ONLINE
    assert rec.state.consecutive_errors == 5

    api.failing = False
    await monitor.run_cycle()
    assert len(sent) == 1


async def test_outage_on_offline_player_then_online_notifies_once(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    monitor = make_monitor()
    await monitor.run_cycle()
    api.failing = True
    await monitor.run_cycle()
    api.failing = False
    api.online["Smash"] = [GER3]
    await monitor.run_cycle()
    await monitor.run_cycle()
    assert len(sent) == 1


async def test_restart_while_online_does_not_renotify(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    first = make_monitor(startup_strategy=StartupStrategy.RESUME)
    await first.run_cycle()
    api.online["Smash"] = [GER3]
    await first.run_cycle()
    assert len(sent) == 1

    # New process, same database.
    for strategy in StartupStrategy:
        restarted = make_monitor(startup_strategy=strategy)
        await restarted.run_cycle()
        await restarted.run_cycle()
    assert len(sent) == 1


async def test_resume_strategy_notifies_transition_during_downtime(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    await make_monitor().run_cycle()  # persisted: offline
    api.online["Smash"] = [GER3]
    await make_monitor(startup_strategy=StartupStrategy.RESUME).run_cycle()
    assert len(sent) == 1


async def test_baseline_strategy_ignores_transition_during_downtime(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    await make_monitor().run_cycle()
    api.online["Smash"] = [GER3]
    await make_monitor(startup_strategy=StartupStrategy.BASELINE).run_cycle()
    assert sent == []


async def test_concurrent_cycles_do_not_duplicate(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    monitor = make_monitor()
    await monitor.run_cycle()
    api.online["Smash"] = [GER3]
    api.delay = 0.05
    await asyncio.gather(*(monitor.run_cycle() for _ in range(5)))
    assert len(sent) == 1


async def test_player_removed_during_cycle_is_not_notified(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    monitor = make_monitor()
    await monitor.run_cycle()
    api.online["Smash"] = [GER3]
    api.delay = 0.1
    cycle = asyncio.create_task(monitor.run_cycle())
    await asyncio.sleep(0.02)
    await storage.remove_player("Smash")
    await cycle
    assert sent == []


async def test_many_players_coming_online_each_notified_once(env):
    storage, api, sent, make_monitor = env
    names = [f"p{i}" for i in range(10)]
    for n in names:
        await storage.add_player(n)
    monitor = make_monitor()
    await monitor.run_cycle()
    for n in names:
        api.online[n] = [GER3]
    await monitor.run_cycle()
    await monitor.run_cycle()
    assert sorted(n.player_name for n in sent) == sorted(names)


async def test_never_seen_player_counts_as_offline(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("ghost")
    api.unknown.add("ghost")
    r = await make_monitor().run_cycle()
    assert r.errors == 0
    assert (await storage.get_player("ghost")).state.status is Status.OFFLINE


async def test_server_filter(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash", server_filter=GER3.address)
    monitor = make_monitor()
    await monitor.run_cycle()
    api.online["Smash"] = [OTHER]
    await monitor.run_cycle()
    assert sent == []  # online, but not on the preferred server
    api.online["Smash"] = [OTHER, GER3]
    await monitor.run_cycle()
    assert len(sent) == 1 and sent[0].server_name == GER3.name and sent[0].other_servers == 0


async def test_other_servers_counted(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    monitor = make_monitor()
    await monitor.run_cycle()
    api.online["Smash"] = [GER3, OTHER]
    await monitor.run_cycle()
    assert sent[0].other_servers == 1


async def test_disabled_players_are_not_polled(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("Smash")
    await storage.set_enabled("Smash", False)
    await make_monitor().run_cycle()
    assert api.calls == 0


async def test_bulk_strategy_uses_single_request(env):
    storage, api, sent, make_monitor = env
    for n in ("a", "b", "c"):
        await storage.add_player(n)
    monitor = make_monitor(poll_strategy=PollStrategy.BULK)
    await monitor.run_cycle()
    api.online["b"] = [GER3]
    await monitor.run_cycle()
    assert api.bulk_calls == 2 and api.calls == 0
    assert [n.player_name for n in sent] == ["b"]


async def test_bulk_failure_marks_all_unknown_not_offline(env):
    storage, api, sent, make_monitor = env
    await storage.add_player("a")
    monitor = make_monitor(poll_strategy=PollStrategy.BULK)
    api.online["a"] = [GER3]
    await monitor.run_cycle()
    api.failing = True
    r = await monitor.run_cycle()
    assert r.errors == 1
    assert (await storage.get_player("a")).state.status is Status.ONLINE


def test_auto_strategy_respects_request_budget(env):
    _, api, _, make_monitor = env
    monitor = make_monitor(poll_interval_seconds=60)
    assert monitor.choose_strategy(60) is PollStrategy.PER_PLAYER
    assert monitor.choose_strategy(61) is PollStrategy.BULK


async def test_run_forever_survives_errors_and_stops(env, tmp_path):
    storage, api, sent, make_monitor = env
    heartbeat = tmp_path / "hb"
    monitor = make_monitor(poll_interval_seconds=15, heartbeat_file=str(heartbeat))

    calls = 0
    original = monitor.run_cycle

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return await original()

    monitor.run_cycle = flaky  # type: ignore[method-assign]
    task = asyncio.create_task(monitor.run_forever())
    await asyncio.sleep(0.05)
    monitor.stop()
    await asyncio.wait_for(task, 2)
    assert calls == 1 and heartbeat.exists()
