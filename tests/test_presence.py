from datetime import datetime, timedelta, timezone

import pytest

from statustw_bot.config import StartupStrategy
from statustw_bot.presence import Event, Observation, PresenceState, Status, evaluate

T0 = datetime(2026, 8, 1, 1, 30, tzinfo=timezone.utc)
ONLINE = Observation.online_on("GER3 #7", "1.2.3.4:8303")
OFFLINE = Observation.offline()
ERROR = Observation.error()


def step(state, obs, *, first=False, strategy=StartupStrategy.BASELINE, minutes=0, confirmations=2, cooldown=0):
    return evaluate(
        state,
        obs,
        now=T0 + timedelta(minutes=minutes),
        first_observation=first,
        strategy=strategy,
        offline_confirmations=confirmations,
        cooldown=timedelta(seconds=cooldown),
    )


def offline_state() -> PresenceState:
    return PresenceState(status=Status.OFFLINE, offline_streak=3)


def test_offline_to_online_notifies_once():
    t = step(offline_state(), ONLINE)
    assert t.notify and t.event is Event.CAME_ONLINE
    assert t.state.status is Status.ONLINE and t.state.session_announced
    assert t.state.last_server_name == "GER3 #7"
    assert t.state.last_notified_at == T0

    again = step(t.state, ONLINE, minutes=1)
    assert not again.notify and again.event is None


def test_api_error_never_means_offline():
    online = step(offline_state(), ONLINE).state
    t = step(online, ERROR, minutes=1)
    assert t.state.status is Status.ONLINE
    assert t.state.consecutive_errors == 1
    assert not t.notify and t.event is None
    # Recovery while still online: no second notification.
    back = step(t.state, ONLINE, minutes=2)
    assert not back.notify
    assert back.state.consecutive_errors == 0


def test_error_on_offline_player_keeps_offline_and_does_not_notify():
    t = step(offline_state(), ERROR)
    assert t.state.status is Status.OFFLINE and not t.notify


def test_single_missing_snapshot_is_debounced():
    online = step(offline_state(), ONLINE).state
    blip = step(online, OFFLINE, minutes=1)
    assert blip.state.status is Status.ONLINE and blip.event is None
    back = step(blip.state, ONLINE, minutes=2)
    assert not back.notify
    assert back.state.offline_streak == 0


def test_confirmed_offline_then_online_notifies_again():
    s = step(offline_state(), ONLINE).state
    s = step(s, OFFLINE, minutes=1).state
    t = step(s, OFFLINE, minutes=2)
    assert t.event is Event.WENT_OFFLINE and not t.notify
    assert t.state.status is Status.OFFLINE and not t.state.session_announced
    t2 = step(t.state, ONLINE, minutes=3)
    assert t2.notify


def test_offline_confirmations_one_switches_immediately():
    s = step(offline_state(), ONLINE).state
    t = step(s, OFFLINE, minutes=1, confirmations=1)
    assert t.state.status is Status.OFFLINE and t.event is Event.WENT_OFFLINE


@pytest.mark.parametrize("persisted", [Status.UNKNOWN, Status.OFFLINE])
def test_baseline_strategy_never_notifies_on_first_observation(persisted):
    t = step(PresenceState(status=persisted), ONLINE, first=True, strategy=StartupStrategy.BASELINE)
    assert not t.notify
    assert t.state.status is Status.ONLINE and t.state.session_announced
    assert t.suppressed_reason == "startup baseline"


def test_resume_strategy_notifies_only_after_known_offline():
    assert step(offline_state(), ONLINE, first=True, strategy=StartupStrategy.RESUME).notify
    assert not step(PresenceState(), ONLINE, first=True, strategy=StartupStrategy.RESUME).notify


def test_notify_strategy_announces_unknown_but_not_already_online():
    assert step(PresenceState(), ONLINE, first=True, strategy=StartupStrategy.NOTIFY).notify
    already = PresenceState(status=Status.ONLINE, session_announced=True)
    assert not step(already, ONLINE, first=True, strategy=StartupStrategy.NOTIFY).notify


@pytest.mark.parametrize("strategy", list(StartupStrategy))
def test_restart_while_online_never_notifies(strategy):
    persisted = PresenceState(status=Status.ONLINE, session_announced=True)
    assert not step(persisted, ONLINE, first=True, strategy=strategy).notify


def test_first_observation_offline_records_offline():
    t = step(PresenceState(), OFFLINE, first=True)
    assert t.state.status is Status.OFFLINE and not t.notify and t.event is None


def test_cooldown_suppresses_quick_reconnect():
    s = step(offline_state(), ONLINE, cooldown=600).state
    s = step(s, OFFLINE, minutes=1, cooldown=600).state
    s = step(s, OFFLINE, minutes=2, cooldown=600).state
    t = step(s, ONLINE, minutes=3, cooldown=600)
    assert t.event is Event.CAME_ONLINE and not t.notify and t.suppressed_reason == "notify cooldown"
    # Suppressed session is marked as handled -> no late notification.
    assert not step(t.state, ONLINE, minutes=4, cooldown=600).notify
