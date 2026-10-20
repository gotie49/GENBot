import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from statustw_bot.notifier import (
    FOOTER_TEXT,
    ChannelUnavailable,
    LinkTemplates,
    Notification,
    NotificationQueue,
    build_embed,
    escape_text,
    link_text,
)

LINKS = LinkTemplates(
    player_profile="https://status.tw/players/{name}",
    server="https://status.tw/servers/{ip}/{port}",
)
WHEN = datetime(2026, 8, 1, 1, 30, tzinfo=timezone.utc)
N = Notification(
    player_name="Smash",
    detected_at=WHEN,
    server_name="[A] [KoG] GER3 #7 - Solo Gores [kog.tw]",
    server_ip="116.203.164.237",
    server_port=8303,
)


def test_embed_matches_reference_layout():
    embed = build_embed(N, LINKS, 0x701A75)
    assert embed.description == (
        "Player [Smash](https://status.tw/players/Smash) connected to "
        "[[A] [KoG] GER3 #7 - Solo Gores [kog.tw]](https://status.tw/servers/116.203.164.237/8303)"
    )
    assert embed.footer.text == FOOTER_TEXT
    assert embed.timestamp == WHEN
    assert embed.color.value == 0x701A75
    assert embed.title is None and not embed.fields


def test_player_name_is_url_encoded_and_escaped():
    n = Notification(player_name="nameless_tee [x]", detected_at=WHEN, server_name="S", server_ip="1.2.3.4", server_port=1)
    embed = build_embed(n, LINKS, 0)
    assert "[nameless_tee [x]](https://status.tw/players/nameless_tee%20%5Bx%5D)" in embed.description


def test_missing_links_fall_back_to_bold_text():
    embed = build_embed(N, LinkTemplates(), 0)
    assert embed.description.startswith("Player **Smash** connected to **\\[A\\]")
    assert "](" not in embed.description


def test_missing_server_still_renders():
    embed = build_embed(Notification(player_name="Smash", detected_at=WHEN), LINKS, 0)
    assert embed.description == "Player [Smash](https://status.tw/players/Smash) is now online"


def test_profile_override_and_other_servers():
    n = Notification(player_name="Smash", detected_at=WHEN, server_name="S", server_ip="1.2.3.4", server_port=1,
                     profile_url_override="https://example.org/u/smash", other_servers=2)
    d = build_embed(n, LINKS, 0).description
    assert "[Smash](https://example.org/u/smash)" in d and d.endswith("(and 2 other servers)")


def test_escape_text():
    assert escape_text("*_~`|>[]()\\") == r"\*\_\~\`\|\>\[\]\(\)\\"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Verified in a real Discord client (2026-10-20): brackets render fine unescaped.
        ("[A] [KoG] GER3 [kog.tw]", "[A] [KoG] GER3 [kog.tw]"),
        ("nameless_tee", "nameless_tee"),  # intra-word underscore never formats
        ("_KoG_", "ˍKoGˍ"),
        ("*Pro* ~~x~~ `c` ||s||", "∗Pro∗ ∼∼x∼∼ ˋcˋ ǀǀsǀǀ"),
        ("a](b) c", "a](b) c"),
    ],
)
def test_link_text_neutralises_formatting(raw, expected):
    assert link_text(raw) == expected


def test_link_text_contains_no_backslash_escapes():
    assert "\\" not in link_text("[A] *x* _y_ `z`")


def _http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "error")


async def _run_queue(sender, notifications):
    q = NotificationQueue(sender, LINKS, 0, min_interval=0, retry_base_delay=0.001)
    q.start()
    for n in notifications:
        await q.enqueue(n)
    await q.drain(2)
    await q.stop()
    return q


async def test_queue_sends_in_order():
    seen = []

    async def sender(embed):
        seen.append(embed.description)

    q = await _run_queue(sender, [Notification(player_name=f"p{i}", detected_at=WHEN) for i in range(5)])
    assert [d.split("/")[-1].split(")")[0] for d in seen] == [f"p{i}" for i in range(5)]
    assert q.sent == 5


async def test_queue_retries_transient_errors():
    attempts = 0

    async def sender(embed):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _http_error(discord.HTTPException, 503)

    q = await _run_queue(sender, [N])
    assert attempts == 3 and q.sent == 1 and q.failed == 0


@pytest.mark.parametrize("error", [lambda: _http_error(discord.Forbidden, 403), lambda: ChannelUnavailable(1)])
async def test_queue_does_not_retry_permission_errors(error):
    attempts = 0

    async def sender(embed):
        nonlocal attempts
        attempts += 1
        raise error()

    q = await _run_queue(sender, [N, N])
    assert attempts == 2 and q.failed == 2  # one attempt each, worker keeps running


async def test_queue_survives_unexpected_exception():
    calls = 0

    async def sender(embed):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bug")

    q = await _run_queue(sender, [N, N])
    assert calls == 2 and q.sent == 1


async def test_queue_respects_min_interval():
    loop = asyncio.get_running_loop()
    times = []

    async def sender(embed):
        times.append(loop.time())

    q = NotificationQueue(sender, LINKS, 0, min_interval=0.05)
    q.start()
    for _ in range(3):
        await q.enqueue(N)
    await q.drain(2)
    await q.stop()
    assert times[1] - times[0] >= 0.045 and times[2] - times[1] >= 0.045
