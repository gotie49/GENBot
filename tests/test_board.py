from datetime import datetime, timedelta, timezone

import discord
import pytest

from statustw_bot.board import (
    ACCENT_COLOR,
    DOT_DAY,
    DOT_HOURS,
    DOT_NEVER,
    DOT_OLD,
    DOT_ONLINE,
    DOT_RECENT,
    ROWS_PER_MESSAGE,
    MessageMissing,
    StatusBoard,
    build_pages,
    build_view,
    format_ago,
    player_line,
    status_dot,
)
from statustw_bot.notifier import LinkTemplates
from statustw_bot.presence import PresenceState, Status
from statustw_bot.storage import PlayerRecord, Storage

NOW = datetime(2026, 10, 20, 14, 32, tzinfo=timezone.utc)
LINKS = LinkTemplates(
    server="https://status.tw/servers/{ip}/{port}",
    connect="https://gotie49.github.io/GENBot/connect/#{ip}:{port}",
)
SERVER = "[A] |*KoG*| GER3 #26 - Extreme Gores [kog.tw]"


def record(name, *, status=Status.OFFLINE, ago=None, server=SERVER, address="116.203.164.237:8303",
           enabled=True, pid=1):
    has_server = ago is not None or status is Status.ONLINE
    return PlayerRecord(
        id=pid, name=name, enabled=enabled, profile_url=None, server_filter=None, added_by=None, created_at=NOW,
        state=PresenceState(
            status=status,
            last_online_at=None if ago is None else NOW - ago,
            last_server_name=server if has_server else None,
            last_server_address=address if has_server else None,
        ),
    )


# ---------------------------------------------------------------- rendering


@pytest.mark.parametrize(
    ("status", "ago", "dot"),
    [
        (Status.ONLINE, timedelta(0), DOT_ONLINE),
        (Status.OFFLINE, timedelta(minutes=12), DOT_RECENT),
        (Status.OFFLINE, timedelta(hours=2, minutes=15), DOT_HOURS),
        (Status.OFFLINE, timedelta(hours=18), DOT_DAY),
        (Status.OFFLINE, timedelta(days=3), DOT_OLD),
        (Status.OFFLINE, None, DOT_NEVER),
    ],
)
def test_status_dots(status, ago, dot):
    assert status_dot(record("x", status=status, ago=ago), NOW) == dot


@pytest.mark.parametrize(
    ("ago", "text"),
    [
        (timedelta(seconds=20), "just now"),
        (timedelta(minutes=18), "18m ago"),
        (timedelta(hours=2, minutes=15), "2h 15m ago"),
        (timedelta(hours=3), "3h ago"),
        (timedelta(hours=4, minutes=50), "4h 50m ago"),
        (timedelta(hours=18, minutes=40), "18h ago"),
        (timedelta(days=3, hours=5), "3d ago"),
    ],
)
def test_format_ago(ago, text):
    assert format_ago(ago) == text


def test_line_matches_variant_b_with_dot():
    line = player_line(record("Kingjulius", ago=timedelta(minutes=12)), LINKS, NOW)
    assert line == (
        "🟡 **Kingjulius**   [[A] ǀ∗KoG∗ǀ GER3 #26 - Extreme Gores [kog.tw]]"
        "(https://gotie49.github.io/GENBot/connect/#116.203.164.237:8303)   12m ago"
    )


def test_line_fallbacks():
    assert player_line(record("Gotie", status=Status.ONLINE, ago=timedelta(0)), LINKS, NOW).endswith("   online now")
    line = player_line(record("a", ago=timedelta(hours=1)), LinkTemplates(server="https://status.tw/servers/{ip}/{port}"), NOW)
    assert "(https://status.tw/servers/116.203.164.237/8303)" in line
    assert "   \\[A\\] \\|\\*KoG\\*\\|" in player_line(record("a", ago=timedelta(hours=1)), LinkTemplates(), NOW)
    assert player_line(record("newbie"), LINKS, NOW) == "⚫ **newbie**   never"
    assert player_line(record("p", ago=timedelta(hours=1), enabled=False), LINKS, NOW).endswith("   (paused)")
    assert player_line(record("under_score*"), LINKS, NOW).startswith("⚫ **under\\_score\\***")


def test_pages_sorted_and_split():
    players = [record(f"p{i:02}", ago=timedelta(minutes=i + 1), pid=i) for i in range(18)]
    players.append(record("online", status=Status.ONLINE, ago=timedelta(0), pid=99))
    players.append(record("never", pid=100))
    pages = build_pages(players, LINKS, NOW)
    assert [len(p) for p in pages] == [ROWS_PER_MESSAGE, 20 - ROWS_PER_MESSAGE]
    flat = [row for page in pages for row in page]
    assert flat[0].startswith("🟢 **online**")
    assert flat[1].startswith("🟡 **p00**") and flat[-2].startswith("🟡 **p17**")
    assert flat[-1].startswith("⚫ **never**")


def test_empty_board():
    assert build_pages([], LINKS, NOW) == [["No players are being watched yet. Use `/player add`."]]


def test_view_is_one_container_with_dividers_within_discord_limits():
    rows = [f"row {i}" for i in range(ROWS_PER_MESSAGE)]
    view = build_view(rows)
    [container] = view.to_components()
    assert container["type"] == 17 and container["accent_color"] == ACCENT_COLOR
    kinds = [c["type"] for c in container["components"]]
    assert kinds == [10, 14] * (ROWS_PER_MESSAGE - 1) + [10]  # text, divider, text, ...
    assert all(c["spacing"] == 1 and c["divider"] for c in container["components"] if c["type"] == 14)
    assert view.has_components_v2() and view.total_children_count <= 40
    assert build_view(rows, 0xFF8800).to_components()[0]["accent_color"] == 0xFF8800


def test_text_changes_only_once_per_minute():
    rec = record("a", ago=timedelta(minutes=10))
    first = build_pages([rec], LINKS, NOW)
    assert build_pages([rec], LINKS, NOW + timedelta(seconds=30)) == first
    assert build_pages([rec], LINKS, NOW + timedelta(minutes=1)) != first


# ---------------------------------------------------------------- synchronisation


class FakeTransport:
    def __init__(self):
        self.messages: dict[int, list] = {}
        self.next_id = 1000
        self.calls: list[tuple[str, int]] = []

    async def send(self, page):
        self.next_id += 1
        self.messages[self.next_id] = page
        self.calls.append(("send", self.next_id))
        return self.next_id

    async def edit(self, message_id, page):
        if message_id not in self.messages:
            raise MessageMissing(message_id)
        self.messages[message_id] = page
        self.calls.append(("edit", message_id))

    async def delete(self, message_id):
        if self.messages.pop(message_id, None) is None:
            raise MessageMissing(message_id)
        self.calls.append(("delete", message_id))


@pytest.fixture
async def storage(tmp_path):
    s = Storage(str(tmp_path / "bot.sqlite3"))
    await s.open()
    yield s
    await s.close()


def make_board(storage, transport, clock=lambda: NOW):
    return StatusBoard(storage, transport, 555, LINKS, clock=clock)


async def add_players(storage, n):
    for i in range(n):
        await storage.add_player(f"p{i:02}")


async def test_first_refresh_sends_then_edits_only_on_change(storage):
    t = FakeTransport()
    board = make_board(storage, t)
    await add_players(storage, 3)
    r = await board.refresh()
    assert r.sent == 1 and await storage.get_board_messages(555) == [1001]

    assert (await board.refresh()).skipped  # nothing changed -> no Discord call
    assert t.calls == [("send", 1001)]

    await storage.add_player("new")
    r = await board.refresh()
    assert r.edited == 1 and r.sent == 0
    assert len(t.messages[1001]) == 4


async def test_board_survives_restart_without_new_message(storage):
    t = FakeTransport()
    await add_players(storage, 2)
    await make_board(storage, t).refresh()
    restarted = make_board(storage, t)  # new process: in-memory signature is gone
    r = await restarted.refresh()
    assert r.edited == 1 and r.sent == 0 and list(t.messages) == [1001]


async def test_grows_and_shrinks_across_messages(storage):
    t = FakeTransport()
    board = make_board(storage, t)
    await add_players(storage, ROWS_PER_MESSAGE + 2)
    await board.refresh()
    assert await storage.get_board_messages(555) == [1001, 1002]
    assert [len(t.messages[m]) for m in (1001, 1002)] == [ROWS_PER_MESSAGE, 2]

    for i in range(5):
        await storage.remove_player(f"p{i:02}")
    r = await board.refresh()
    assert r.deleted == 1 and await storage.get_board_messages(555) == [1001]
    assert 1002 not in t.messages


async def test_deleted_or_outdated_message_is_recreated_in_order(storage):
    t = FakeTransport()
    board = make_board(storage, t)
    await add_players(storage, ROWS_PER_MESSAGE + 3)
    await board.refresh()
    del t.messages[1001]  # deleted by someone, or an old-format message the transport removed
    await storage.add_player("zz")
    await board.refresh()
    assert await storage.get_board_messages(555) == [1003, 1004]  # page order preserved
    assert 1002 not in t.messages


async def test_dot_change_over_time_triggers_edit(storage):
    t = FakeTransport()
    now = [NOW]
    board = make_board(storage, t, clock=lambda: now[0])
    rec = await storage.add_player("a")
    await storage.save_state(rec.id, PresenceState(status=Status.OFFLINE, last_online_at=NOW - timedelta(minutes=50)))
    await board.refresh()
    now[0] = NOW + timedelta(seconds=40)  # same displayed minute -> no Discord call
    assert (await board.refresh()).skipped
    now[0] = NOW + timedelta(minutes=15)  # 65 min: crosses the 1 h bucket
    assert (await board.refresh()).edited == 1
    assert t.messages[1001][0].startswith(DOT_HOURS)


async def test_failure_keeps_already_sent_ids(storage):
    class Flaky(FakeTransport):
        async def send(self, page):
            if len(self.messages) == 1:
                raise RuntimeError("discord down")
            return await super().send(page)

    t = Flaky()
    await add_players(storage, ROWS_PER_MESSAGE + 2)
    with pytest.raises(RuntimeError):
        await make_board(storage, t).refresh()
    assert await storage.get_board_messages(555) == [1001]  # no duplicate on the next attempt


async def test_channel_transport_replaces_old_format_message():
    """A 400 on edit (old embed board -> Components V2) deletes the message and reports it missing."""
    from statustw_bot.board import ChannelBoardTransport
    from types import SimpleNamespace

    deleted = []

    class Msg:
        async def edit(self, **kwargs):
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "invalid form body")

        async def delete(self):
            deleted.append(True)

    class Channel(discord.abc.Messageable):
        async def _get_channel(self):
            return self

        def get_partial_message(self, message_id):
            return Msg()

    async def resolve():
        return Channel()

    with pytest.raises(MessageMissing):
        await ChannelBoardTransport(resolve).edit(1, ["row"])
    assert deleted == [True]
