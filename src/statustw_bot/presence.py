"""Pure state-transition logic for player presence. No I/O, fully unit-testable.

Rules
-----
* A failed lookup (``Observation.error``) never changes ``status``; it only counts errors.
* offline -> online raises ``CAME_ONLINE``; a notification is sent at most once per
  online session (``session_announced``).
* online -> offline is only accepted after ``offline_confirmations`` consecutive offline
  observations (debounces server switches / single missing snapshots).
* The first successful observation of a player in this process is handled according
  to the configured :class:`StartupStrategy`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from .config import StartupStrategy


class Status(str, Enum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"


class Event(str, Enum):
    CAME_ONLINE = "came_online"
    WENT_OFFLINE = "went_offline"


@dataclass(frozen=True)
class PresenceState:
    status: Status = Status.UNKNOWN
    status_changed_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_online_at: datetime | None = None
    last_server_name: str | None = None
    last_server_address: str | None = None
    offline_streak: int = 0
    consecutive_errors: int = 0
    # True once the current online session was announced (or deliberately suppressed).
    session_announced: bool = False
    last_notified_at: datetime | None = None


@dataclass(frozen=True)
class Observation:
    """Result of one presence lookup for one player."""

    online: bool | None  # None = lookup failed / unknown
    server_name: str | None = None
    server_address: str | None = None

    @classmethod
    def error(cls) -> "Observation":
        return cls(online=None)

    @classmethod
    def offline(cls) -> "Observation":
        return cls(online=False)

    @classmethod
    def online_on(cls, server_name: str | None, server_address: str | None) -> "Observation":
        return cls(online=True, server_name=server_name, server_address=server_address)


@dataclass(frozen=True)
class Transition:
    state: PresenceState
    event: Event | None = None
    notify: bool = False
    suppressed_reason: str | None = None


def evaluate(
    state: PresenceState,
    observation: Observation,
    *,
    now: datetime,
    first_observation: bool,
    strategy: StartupStrategy,
    offline_confirmations: int = 2,
    cooldown: timedelta = timedelta(0),
) -> Transition:
    if observation.online is None:
        return Transition(replace(state, consecutive_errors=state.consecutive_errors + 1))

    base = replace(state, last_checked_at=now, consecutive_errors=0)

    if observation.online:
        return _observe_online(base, observation, now, first_observation, strategy, cooldown)
    return _observe_offline(base, now, offline_confirmations)


def _observe_online(
    state: PresenceState,
    obs: Observation,
    now: datetime,
    first_observation: bool,
    strategy: StartupStrategy,
    cooldown: timedelta,
) -> Transition:
    online_state = replace(
        state,
        last_online_at=now,
        last_server_name=obs.server_name,
        last_server_address=obs.server_address,
        offline_streak=0,
    )

    if state.status is Status.ONLINE:
        # Still in the same session (also across a restart). Never re-announce.
        return Transition(online_state)

    new_session = replace(online_state, status=Status.ONLINE, status_changed_at=now, session_announced=False)

    reason: str | None = None
    if first_observation:
        if strategy is StartupStrategy.BASELINE:
            reason = "startup baseline"
        elif strategy is StartupStrategy.RESUME and state.status is not Status.OFFLINE:
            reason = "startup resume: no known previous offline state"
        # NOTIFY: announce (session_announced is False here because status was not ONLINE).
    elif state.status is Status.UNKNOWN:
        reason = "no previous known state"

    if reason is None and cooldown > timedelta(0) and state.last_notified_at is not None:
        if now - state.last_notified_at < cooldown:
            reason = "notify cooldown"

    if reason is not None:
        return Transition(replace(new_session, session_announced=True), Event.CAME_ONLINE, False, reason)
    return Transition(
        replace(new_session, session_announced=True, last_notified_at=now),
        Event.CAME_ONLINE,
        True,
    )


def _observe_offline(state: PresenceState, now: datetime, offline_confirmations: int) -> Transition:
    if state.status is Status.ONLINE:
        streak = state.offline_streak + 1
        if streak < offline_confirmations:
            return Transition(replace(state, offline_streak=streak))
        return Transition(
            replace(
                state,
                status=Status.OFFLINE,
                status_changed_at=now,
                offline_streak=streak,
                session_announced=False,
            ),
            Event.WENT_OFFLINE,
        )
    if state.status is Status.OFFLINE:
        return Transition(replace(state, offline_streak=state.offline_streak + 1))
    return Transition(
        replace(state, status=Status.OFFLINE, status_changed_at=now, offline_streak=1, session_announced=False)
    )
