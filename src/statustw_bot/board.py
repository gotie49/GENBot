"""Persistent, continuously updated status board in a dedicated channel.

One embed per player (Discord allows 10 per message, so larger lists span several
messages). The left border colour encodes how recently the player was online:

    green  online now or < 1 h      yellow 1–5 h      red 5–24 h      white > 24 h

Every embed carries the same invisible 1000x1 px image so that Discord renders all
bars at full width (embeds otherwise shrink to their text). Times are short texts
written by the bot ("18m ago", "2h 15m ago"); the bot only edits when the rendered
content changes, which is at most about once a minute.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Protocol, Sequence

import discord

from .logging_setup import fields
from .notifier import LinkTemplates, escape_text, link_text
from .presence import Status
from .storage import PlayerRecord, Storage

log = logging.getLogger(__name__)

EMBEDS_PER_MESSAGE = 10  # Discord limit

COLOR_RECENT = 0x23A55A  # online or < 1 h
COLOR_HOURS = 0xF0B232  # 1–5 h
COLOR_DAY = 0xF23F43  # 5–24 h
COLOR_OLD = 0xDBDEE1  # > 24 h
COLOR_NEVER = 0x4E5058  # never seen online

SPACER_FILENAME = "spacer.png"


def _spacer_png(width: int = 1000, height: int = 1) -> bytes:
    """Fully transparent PNG; generated in code so no asset has to be shipped or hosted."""
    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


SPACER_PNG = _spacer_png()


def spacer_file() -> discord.File:
    return discord.File(io.BytesIO(SPACER_PNG), filename=SPACER_FILENAME)


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


def color_for(player: PlayerRecord, now: datetime) -> int:
    if player.state.status is Status.ONLINE:
        return COLOR_RECENT
    last = player.state.last_online_at
    if last is None:
        return COLOR_NEVER
    age = now - last
    if age < timedelta(hours=1):
        return COLOR_RECENT
    if age < timedelta(hours=5):
        return COLOR_HOURS
    if age < timedelta(hours=24):
        return COLOR_DAY
    return COLOR_OLD


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
    parts = [f"**{escape_text(player.name)}**"]

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


def _row(description: str, color: int) -> discord.Embed:
    embed = discord.Embed(description=description, color=color)
    embed.set_image(url=f"attachment://{SPACER_FILENAME}")
    return embed


def build_pages(players: Sequence[PlayerRecord], links: LinkTemplates, now: datetime) -> list[list[discord.Embed]]:
    """Render the board: a list of messages, each a list of up to 10 embeds (one per player)."""
    if not players:
        embeds = [_row("No players are being watched yet. Use `/player add`.", COLOR_NEVER)]
    else:
        embeds = [_row(player_line(p, links, now), color_for(p, now)) for p in sorted(players, key=_sort_key)]
    return [embeds[i : i + EMBEDS_PER_MESSAGE] for i in range(0, len(embeds), EMBEDS_PER_MESSAGE)]


def _signature(pages: list[list[discord.Embed]]) -> str:
    return json.dumps([[e.to_dict() for e in page] for page in pages], sort_keys=True)


class MessageMissing(Exception):
    """The board message no longer exists (deleted by someone)."""


class BoardTransport(Protocol):
    async def send(self, embeds: list[discord.Embed]) -> int: ...

    async def edit(self, message_id: int, embeds: list[discord.Embed]) -> None: ...

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
    """BoardTransport backed by a Discord text channel."""

    def __init__(self, resolve_channel: Callable[[], Awaitable[discord.abc.Messageable | None]]) -> None:
        self._resolve_channel = resolve_channel

    async def _channel(self) -> discord.abc.Messageable:
        channel = await self._resolve_channel()
        if channel is None or not hasattr(channel, "get_partial_message"):
            raise RuntimeError("status channel unavailable (check DISCORD_STATUS_CHANNEL_ID and bot permissions)")
        return channel

    async def send(self, embeds: list[discord.Embed]) -> int:
        message = await (await self._channel()).send(embeds=embeds, file=spacer_file())
        return message.id

    async def edit(self, message_id: int, embeds: list[discord.Embed]) -> None:
        channel = await self._channel()
        try:
            # Re-attach the spacer on every edit so boards created by older versions get it too.
            await channel.get_partial_message(message_id).edit(  # type: ignore[attr-defined]
                embeds=embeds, attachments=[spacer_file()]
            )
        except discord.NotFound as exc:
            raise MessageMissing(message_id) from exc

    async def delete(self, message_id: int) -> None:
        channel = await self._channel()
        try:
            await channel.get_partial_message(message_id).delete()  # type: ignore[attr-defined]
        except discord.NotFound as exc:
            raise MessageMissing(message_id) from exc
