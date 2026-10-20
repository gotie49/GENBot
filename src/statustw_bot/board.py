"""Persistent, continuously updated status board in a dedicated channel.

Layout (chosen after comparing variants in a real Discord client): one Components-V2
container per message, one text row per player, rows separated by thin dividers:

    🟢 **Gotie**   [A] |KoG| GER3 #26 - Insane Gores [kog.tw]   online now
    ───────────────────────────────────────────────────────────────────────
    🟠 **KOF**     [A] |KoG| GER3 #29 - Mods Gores [kog.tw]     2h 15m ago

The dot shows how recently the player was online: 🟢 online now, 🟡 < 1 h, 🟠 1–5 h,
🔴 5–24 h, ⚪ > 24 h, ⚫ never seen. Server names link to CONNECT_URL_TEMPLATE
(forwarding to ddnet://). Times are short texts written by the bot; the message is
only edited when the rendered text changes, i.e. at most about once a minute.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Protocol, Sequence

import discord

from .logging_setup import fields
from .notifier import LinkTemplates, escape_text, link_text
from .presence import Status
from .storage import PlayerRecord, Storage

log = logging.getLogger(__name__)

# Components V2 allow 40 components per message; each row costs a text display plus a
# separator, so 15 rows per message keep a wide margin (and match the expected list size).
ROWS_PER_MESSAGE = 15
ACCENT_COLOR = 0x5865F2  # default for BOARD_COLOR

DOT_ONLINE = "🟢"  # currently online
DOT_RECENT = "🟡"  # offline for < 1 h
DOT_HOURS = "🟠"  # 1–5 h
DOT_DAY = "🔴"  # 5–24 h
DOT_OLD = "⚪"  # > 24 h
DOT_NEVER = "⚫"  # never seen online

EMPTY_TEXT = "No players are being watched yet. Use `/player add`."

Page = list[str]  # the text rows of one board message


def format_ago(delta: timedelta) -> str:
    """Compact age like the design: 18m ago, 2h 15m ago, 18h ago, 3d ago."""
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours, rest = divmod(minutes, 60)
    if hours < 5:
        return f"{hours}h {rest}m ago" if rest else f"{hours}h ago"
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def status_dot(player: PlayerRecord, now: datetime) -> str:
    if player.state.status is Status.ONLINE:
        return DOT_ONLINE
    last = player.state.last_online_at
    if last is None:
        return DOT_NEVER
    age = now - last
    if age < timedelta(hours=1):
        return DOT_RECENT
    if age < timedelta(hours=5):
        return DOT_HOURS
    if age < timedelta(hours=24):
        return DOT_DAY
    return DOT_OLD


def _sort_key(player: PlayerRecord) -> tuple[int, float, str]:
    online = player.state.status is Status.ONLINE
    last = player.state.last_online_at
    return (0 if online else 1, -(last.timestamp() if last else float("-inf")), player.name.lower())


def _split_address(address: str | None) -> tuple[str | None, int | None]:
    if not address:
        return None, None
    ip, _, port = address.rpartition(":")
    return (ip, int(port)) if ip and port.isdigit() else (None, None)


def player_line(player: PlayerRecord, links: LinkTemplates, now: datetime) -> str:
    state = player.state
    parts = [f"{status_dot(player, now)} **{escape_text(player.name)}**"]

    if state.last_server_name:
        ip, port = _split_address(state.last_server_address)
        url = links.connect_url(ip, port) or links.server_url(ip, port)
        parts.append(f"[{link_text(state.last_server_name)}]({url})" if url else escape_text(state.last_server_name))

    if state.status is Status.ONLINE:
        parts.append("online now")
    elif state.last_online_at is not None:
        parts.append(format_ago(now - state.last_online_at))
    else:
        parts.append("never")

    if not player.enabled:
        parts.append("(paused)")
    return "   ".join(parts)


def build_pages(players: Sequence[PlayerRecord], links: LinkTemplates, now: datetime) -> list[Page]:
    """Render the board: one list of text rows per message."""
    if not players:
        return [[EMPTY_TEXT]]
    rows = [player_line(p, links, now) for p in sorted(players, key=_sort_key)]
    return [rows[i : i + ROWS_PER_MESSAGE] for i in range(0, len(rows), ROWS_PER_MESSAGE)]


def build_view(page: Page, accent_color: int = ACCENT_COLOR) -> discord.ui.LayoutView:
    """Components-V2 message: one container, rows separated by small dividers."""
    container: discord.ui.Container = discord.ui.Container(accent_colour=accent_color)
    for index, row in enumerate(page):
        if index:
            container.add_item(discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(row))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def _signature(pages: list[Page]) -> str:
    return json.dumps(pages)


class MessageMissing(Exception):
    """The board message no longer exists (or cannot be edited into the current format)."""


class BoardTransport(Protocol):
    async def send(self, page: Page) -> int: ...

    async def edit(self, message_id: int, page: Page) -> None: ...

    async def delete(self, message_id: int) -> None: ...


@dataclass
class BoardSync:
    sent: int = 0
    edited: int = 0
    deleted: int = 0
    skipped: bool = False


class StatusBoard:
    def __init__(
        self,
        storage: Storage,
        transport: BoardTransport,
        channel_id: int,
        links: LinkTemplates,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        periodic_seconds: float = 60.0,
        debounce_seconds: float = 2.0,
        ready: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._storage = storage
        self._transport = transport
        self._channel_id = channel_id
        self._links = links
        self._clock = clock
        self._periodic = periodic_seconds
        self._debounce = debounce_seconds
        self._ready = ready
        self._lock = asyncio.Lock()
        self._dirty = asyncio.Event()
        self._last_signature: str | None = None
        self._task: asyncio.Task[None] | None = None

    def request_refresh(self) -> None:
        self._dirty.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="status-board")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        if self._ready is not None:
            await self._ready()
        log.info("status board started", extra=fields(channel=self._channel_id))
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep running; retried on the next trigger
                self._last_signature = None
                log.error("status board update failed", extra=fields(channel=self._channel_id, error=repr(exc)))
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=self._periodic)
                await asyncio.sleep(self._debounce)  # coalesce bursts (e.g. several commands)
            except asyncio.TimeoutError:
                pass  # periodic refresh: colour buckets change with time
            self._dirty.clear()

    async def refresh(self) -> BoardSync:
        async with self._lock:
            players = await self._storage.list_players()
            pages = build_pages(players, self._links, self._clock())
            signature = _signature(pages)
            if signature == self._last_signature:
                return BoardSync(skipped=True)

            stored = await self._storage.get_board_messages(self._channel_id)
            result = BoardSync()
            current: list[int] = []
            try:
                for index, page in enumerate(pages):
                    if index < len(stored):
                        try:
                            await self._transport.edit(stored[index], page)
                            current.append(stored[index])
                            result.edited += 1
                            continue
                        except MessageMissing:
                            # Keep the page order: drop this and all later messages, then re-post them.
                            for old in stored[index + 1 :]:
                                await self._delete_quietly(old)
                                result.deleted += 1
                            stored = stored[:index]
                    current.append(await self._transport.send(page))
                    result.sent += 1
                for extra in stored[len(pages) :]:
                    await self._delete_quietly(extra)
                    result.deleted += 1
                stored = []
            finally:
                # Persist whatever exists now, even after a partial failure, so nothing is duplicated.
                await self._storage.set_board_messages(self._channel_id, current + stored[len(current) :])

            self._last_signature = signature
            if result.sent or result.deleted:
                log.info("status board updated", extra=fields(players=len(players), pages=len(pages),
                                                              sent=result.sent, edited=result.edited,
                                                              deleted=result.deleted))
            return result

    async def _delete_quietly(self, message_id: int) -> None:
        try:
            await self._transport.delete(message_id)
        except MessageMissing:
            pass


class ChannelBoardTransport:
    """BoardTransport backed by a Discord text channel (Components V2 messages)."""

    def __init__(
        self,
        resolve_channel: Callable[[], Awaitable[discord.abc.Messageable | None]],
        accent_color: int = ACCENT_COLOR,
    ) -> None:
        self._resolve_channel = resolve_channel
        self._accent_color = accent_color

    async def _channel(self) -> discord.abc.Messageable:
        channel = await self._resolve_channel()
        if channel is None or not hasattr(channel, "get_partial_message"):
            raise RuntimeError("status channel unavailable (check DISCORD_STATUS_CHANNEL_ID and bot permissions)")
        return channel

    async def send(self, page: Page) -> int:
        message = await (await self._channel()).send(view=build_view(page, self._accent_color))
        return message.id

    async def edit(self, message_id: int, page: Page) -> None:
        message = (await self._channel()).get_partial_message(message_id)  # type: ignore[attr-defined]
        try:
            await message.edit(view=build_view(page, self._accent_color))
        except discord.NotFound as exc:
            raise MessageMissing(message_id) from exc
        except discord.HTTPException as exc:
            if exc.status != 400:
                raise
            # A board posted by an older version (embeds) cannot be turned into a Components-V2
            # message: remove it so the board logic posts a fresh one in its place.
            log.info("replacing status board message from an older version", extra=fields(message=message_id))
            try:
                await message.delete()
            except discord.NotFound:
                pass
            raise MessageMissing(message_id) from exc

    async def delete(self, message_id: int) -> None:
        channel = await self._channel()
        try:
            await channel.get_partial_message(message_id).delete()  # type: ignore[attr-defined]
        except discord.NotFound as exc:
            raise MessageMissing(message_id) from exc
