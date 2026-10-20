from datetime import datetime, timedelta, timezone

import pytest

from statustw_bot.board import (
    COLOR_DAY,
    COLOR_HOURS,
    COLOR_NEVER,
    COLOR_OLD,
    COLOR_RECENT,
    MessageMissing,
    StatusBoard,
    build_pages,
    color_for,
    format_ago,
    player_line,
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
    return PlayerRecord(
        id=pid, name=name, enabled=enabled, profile_url=None, server_filter=None, added_by=None, created_at=NOW,
        state=PresenceState(
            status=status,
            last_online_at=None if ago is None else NOW - ago,
            last_server_name=server if ago is not None or status is Status.ONLINE else None,
            last_server_address=address if ago is not None or status is Status.ONLINE else None,
        ),
    )


@pytest.mark.parametrize(
    ("status", "ago", "color"),
    [
        (Status.ONLINE, timedelta(0), COLOR_RECENT),
        (Status.OFFLINE, timedelta(minutes=12), COLOR_RECENT),
        (Status.OFFLINE, timedelta(hours=2, minutes=15), COLOR_HOURS),
        (Status.OFFLINE, timedelta(hours=18), COLOR_DAY),
        (Status.OFFLINE, timedelta(days=3), COLOR_OLD),
        (Status.OFFLINE, None, COLOR_NEVER),
    ],
)
def test_color_buckets(status, ago, color):
    assert color_for(record("x", status=status, ago=ago), NOW) == color


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


def test_offline_line_has_connect_link_and_short_time():
    line = player_line(record("Kingjulius", ago=timedelta(minutes=12)), LINKS, NOW)
    assert line == (
        "**Kingjulius**   [[A] ǀ∗KoG∗ǀ GER3 #26 - Extreme Gores [kog.tw]]"
        "(https://gotie49.github.io/GENBot/connect/#116.203.164.237:8303)   12m ago"
    )


def test_lines_contain_no_emoji():
    rows = [
        record("a", status=Status.ONLINE, ago=timedelta(0)),
        record("b", ago=timedelta(hours=2)),
        record("c"),
        record("d", ago=timedelta(hours=1), enabled=False),
    ]
    for r in rows:
        assert all(ord(ch) < 0x2000 or ch in "ǀ∗∼ˋˍ" for ch in player_line(r, LINKS, NOW)), r.name


def test_online_line_and_fallbacks():
    assert player_line(record("Gotie", status=Status.ONLINE, ago=timedelta(0)), LINKS, NOW).endswith("   online now")
    # Without a connect template the status.tw server page is used.
    line = player_line(record("a", ago=timedelta(hours=1)), LinkTemplates(server="https://status.tw/servers/{ip}/{port}"), NOW)
    assert "(https://status.tw/servers/116.203.164.237/8303)" in line
    # Without any template the server name is plain (escaped) text.
    assert "   \\[A\\] \\|\\*KoG\\*\\|" in player_line(record("a", ago=timedelta(hours=1)), LinkTemplates(), NOW)
    assert player_line(record("new", status=Status.UNKNOWN), LINKS, NOW) == "**new**   never"
    assert player_line(record("ghost"), LINKS, NOW) == "**ghost**   never"
    assert player_line(record("p", ago=timedelta(hours=1), enabled=False), LINKS, NOW).endswith("   (paused)")
    assert player_line(record("under_score*"), LINKS, NOW).startswith("**under\\_score\\***")


def test_pages_sorted_chunked_full_width_and_without_legend():
    players = [record(f"p{i:02}", ago=timedelta(minutes=i + 1), pid=i) for i in range(23)]
    players.append(record("online", status=Status.ONLINE, ago=timedelta(0), pid=99))
    players.append(record("never", pid=100))
    pages = build_pages(players, LINKS, NOW)
    assert [len(p) for p in pages] == [10, 10, 5]
    flat = [e for page in pages for e in page]
    assert flat[0].description.startswith("**online**")
    assert flat[1].description.startswith("**p00**") and flat[-2].description.startswith("**p22**")
    assert flat[-1].description.startswith("**never**")
    assert all(e.footer.text is None for e in flat)
    assert all(e.image.url == "attachment://spacer.png" for e in flat)


def test_empty_board():
    pages = build_pages([], LinkTemplates(), NOW)
    assert len(pages) == 1 and "No players" in pages[0][0].description


def test_time_text_changes_trigger_edits_only_per_minute():
    rec = record("a", ago=timedelta(minutes=10))
    first = build_pages([rec], LINKS, NOW)[0][0].description
    assert build_pages([rec], LINKS, NOW + timedelta(seconds=30))[0][0].description == first
    assert build_pages([rec], LINKS, NOW + timedelta(minutes=1))[0][0].description != first


class FakeTransport:
    def __init__(self):
        self.messages: dict[int, list] = {}
        self.next_id = 1000
        self.calls: list[tuple[str, int]] = []

    async def send(self, embeds):
        self.next_id += 1
        self.messages[self.next_id] = embeds
        self.calls.append(("send", self.next_id))
        return self.next_id

    async def edit(self, message_id, embeds):
        if message_id not in self.messages:
            raise MessageMissing(message_id)
        self.messages[message_id] = embeds
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
    await add_players(storage, 12)
    await board.refresh()
    assert await storage.get_board_messages(555) == [1001, 1002]
    assert [len(t.messages[m]) for m in (1001, 1002)] == [10, 2]

    for i in range(5):
        await storage.remove_player(f"p{i:02}")
    r = await board.refresh()
    assert r.deleted == 1 and await storage.get_board_messages(555) == [1001]
    assert 1002 not in t.messages


async def test_deleted_message_is_recreated_in_order(storage):
    t = FakeTransport()
    board = make_board(storage, t)
    await add_players(storage, 15)
    await board.refresh()
    del t.messages[1001]  # someone deleted the first page
    await storage.add_player("zz")
    await board.refresh()
    ids = await storage.get_board_messages(555)
    assert ids == [1003, 1004]  # both pages re-posted so page 1 stays above page 2
    assert 1002 not in t.messages


async def test_colour_change_over_time_triggers_edit(storage):
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
    assert t.messages[1001][0].color.value == COLOR_HOURS


async def test_failure_keeps_already_sent_ids(storage):
    class Flaky(FakeTransport):
        async def send(self, embeds):
            if len(self.messages) == 1:
                raise RuntimeError("discord down")
            return await super().send(embeds)

    t = Flaky()
    await add_players(storage, 12)
    with pytest.raises(RuntimeError):
        await make_board(storage, t).refresh()
    assert await storage.get_board_messages(555) == [1001]  # no duplicate on the next attempt
