"""Periodic presence polling: fetch -> evaluate transitions -> persist -> enqueue notifications."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import PollStrategy, StartupStrategy
from .logging_setup import fields
from .notifier import Notification
from .presence import Event, Observation, evaluate
from .statustw import PlayerNotFound, PlayerPresence, StatusTwClient, StatusTwError
from .storage import PlayerRecord, Storage

log = logging.getLogger(__name__)

NotifyCallback = Callable[[Notification], Awaitable[None]]


@dataclass
class CycleResult:
    strategy: PollStrategy
    checked: int = 0
    errors: int = 0
    online: int = 0
    notifications: int = 0
    events: list[tuple[str, Event]] = field(default_factory=list)


@dataclass(frozen=True)
class MonitorOptions:
    poll_interval_seconds: int = 60
    poll_strategy: PollStrategy = PollStrategy.AUTO
    startup_strategy: StartupStrategy = StartupStrategy.BASELINE
    offline_confirmations: int = 2
    notify_cooldown_seconds: int = 0
    heartbeat_file: str | None = None


class PresenceMonitor:
    def __init__(
        self,
        storage: Storage,
        api: StatusTwClient,
        notify: NotifyCallback,
        options: MonitorOptions,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_cycle: Callable[[], None] | None = None,
    ) -> None:
        self._storage = storage
        self._on_cycle = on_cycle
        self._api = api
        self._notify = notify
        self._options = options
        self._clock = clock
        # Players that had at least one successful observation in this process.
        self._observed: set[int] = set()
        # Serialises cycles (the loop and any manual trigger) so no two can race on the same state.
        self._cycle_lock = asyncio.Lock()
        self._stop = asyncio.Event()

    def forget(self, player_id: int) -> None:
        """Treat the next observation of this player as a first observation (after add/enable)."""
        self._observed.discard(player_id)

    def stop(self) -> None:
        self._stop.set()

    def choose_strategy(self, player_count: int) -> PollStrategy:
        if self._options.poll_strategy is not PollStrategy.AUTO:
            return self._options.poll_strategy
        capacity = self._api.budget.max_requests * self._options.poll_interval_seconds / 60
        return PollStrategy.PER_PLAYER if player_count <= capacity else PollStrategy.BULK

    async def run_forever(self) -> None:
        interval = self._options.poll_interval_seconds
        log.info(
            "presence monitor started",
            extra=fields(interval=interval, strategy=self._options.poll_strategy.value,
                         startup=self._options.startup_strategy.value),
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                result = await self.run_cycle()
                log.debug(
                    "poll cycle finished",
                    extra=fields(strategy=result.strategy.value, checked=result.checked, online=result.online,
                                 errors=result.errors, notifications=result.notifications,
                                 seconds=f"{time.monotonic() - started:.1f}"),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll cycle failed unexpectedly")
            self._write_heartbeat()
            if self._on_cycle is not None:
                self._on_cycle()
            remaining = max(1.0, interval - (time.monotonic() - started))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
        log.info("presence monitor stopped")

    async def run_cycle(self) -> CycleResult:
        async with self._cycle_lock:
            players = await self._storage.list_players(enabled_only=True)
            # Every name (main name and aliases) costs one request in per-player mode.
            strategy = self.choose_strategy(sum(len(p.names) for p in players))
            result = CycleResult(strategy=strategy)
            if not players:
                return result

            if strategy is PollStrategy.BULK:
                presences = await self._fetch_bulk(players)
            else:
                presences = await self._fetch_each(players)

            for player in players:
                await self._process(player, presences.get(player.id), result)
            return result

    async def _fetch_bulk(self, players: list[PlayerRecord]) -> dict[int, PlayerPresence | None]:
        try:
            online = await self._api.list_online_players()
        except StatusTwError as exc:
            log.warning("status.tw bulk lookup failed; status unknown this cycle", extra=fields(error=str(exc)))
            return {}
        return {p.id: _combine(p, [online.get(n, PlayerPresence(n, ())) for n in p.names]) for p in players}

    async def _fetch_each(self, players: list[PlayerRecord]) -> dict[int, PlayerPresence | None]:
        result: dict[int, PlayerPresence | None] = {}
        for player in players:
            lookups: list[PlayerPresence | None] = []
            for name in player.names:
                if self._stop.is_set():
                    return result
                try:
                    lookups.append(await self._api.get_player(name))
                except PlayerNotFound:
                    # Documented: 404 only if the name was never seen -> certainly not online.
                    lookups.append(PlayerPresence(name, ()))
                except StatusTwError as exc:
                    log.warning("status.tw lookup failed; status unknown",
                                extra=fields(player=player.name, name=name, error=str(exc)))
                    lookups.append(None)
            result[player.id] = _combine(player, lookups)
        return result

    async def _process(self, player: PlayerRecord, presence: PlayerPresence | None, result: CycleResult) -> None:
        now = self._clock()
        connections = presence.connections_to(player.server_filter) if presence is not None else ()
        if presence is None:
            observation = Observation.error()
        elif connections:
            first = connections[0]
            observation = Observation.online_on(first.server.name, first.server.address, first.player_name)
        else:
            observation = Observation.offline()

        transition = evaluate(
            player.state,
            observation,
            now=now,
            first_observation=player.id not in self._observed,
            strategy=self._options.startup_strategy,
            offline_confirmations=self._options.offline_confirmations,
            cooldown=timedelta(seconds=self._options.notify_cooldown_seconds),
        )

        # Persist BEFORE notifying: the stored `session_announced` flag is what prevents
        # duplicates after a crash/restart. Not saved => removed/disabled meanwhile => no notify.
        if not await self._storage.save_state(player.id, transition.state):
            log.debug("player changed during poll, result discarded", extra=fields(player=player.name))
            return

        if observation.online is None:
            result.errors += 1
            return
        self._observed.add(player.id)
        result.checked += 1
        result.online += int(bool(observation.online))

        if transition.event is not None:
            result.events.append((player.name, transition.event))
            log.info(
                "presence transition",
                extra=fields(player=player.name, event=transition.event.value,
                             server=observation.server_name or "-", address=observation.server_address or "-",
                             notify=transition.notify, suppressed=transition.suppressed_reason or "-"),
            )
        if transition.notify and connections:
            server = connections[0].server
            await self._notify(
                Notification(
                    player_name=player.name,
                    detected_at=now,
                    server_name=server.name,
                    server_ip=server.ip,
                    server_port=server.port,
                    profile_url_override=player.profile_url,
                    other_servers=len({c.server.address for c in connections}) - 1,
                    seen_name=connections[0].player_name,
                )
            )
            result.notifications += 1

    def _write_heartbeat(self) -> None:
        path = self._options.heartbeat_file
        if not path:
            return
        try:
            with open(path, "w", encoding="ascii") as fh:
                fh.write(str(int(time.time())))
        except OSError as exc:
            log.warning("could not write heartbeat file", extra=fields(path=path, error=str(exc)))


def _combine(player: PlayerRecord, lookups: list[PlayerPresence | None]) -> PlayerPresence | None:
    """Merge the lookups of all names of one person (main name first).

    Online as soon as any name is online. If no name is online and at least one lookup
    failed, the result is unknown (None) - a failed lookup must never mean offline.
    """
    connections = tuple(c for presence in lookups if presence is not None for c in presence.connections)
    if not connections and any(presence is None for presence in lookups):
        return None
    return PlayerPresence(player.name, connections)


__all__ = ["CycleResult", "MonitorOptions", "PresenceMonitor"]
