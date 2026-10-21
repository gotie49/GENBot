"""Building and sending the "player connected" message.

Layout follows the reference screenshot:

    | Player [Smash](profile) connected to [[A] [KoG] GER3 #7 - Solo Gores [kog.tw]](server)
    | **Powered by status.tw** • 8/1/2026 1:30 AM          (footer + embed timestamp)

Left border colour #701A75 (sampled from the reference image).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from urllib.parse import quote

import discord

from .logging_setup import fields

log = logging.getLogger(__name__)

FOOTER_TEXT = "Powered by status.tw"
SEND_ATTEMPTS = 3
QUEUE_MAX_SIZE = 500

_LINK_TEXT_SPECIAL = re.compile(r"([\[\]\\*_~`|>()])")


class ChannelUnavailable(Exception):
    """The configured notification channel cannot be resolved (wrong ID or no access)."""


@dataclass(frozen=True)
class Notification:
    player_name: str
    detected_at: datetime
    server_name: str | None = None
    server_ip: str | None = None
    server_port: int | None = None
    profile_url_override: str | None = None
    other_servers: int = 0
    seen_name: str | None = None  # alias the person is online with, if not the main name


@dataclass(frozen=True)
class LinkTemplates:
    """URL patterns for links. ``None`` disables the link (plain text is shown instead)."""

    player_profile: str | None = None  # placeholders: {name}
    server: str | None = None  # placeholders: {ip}, {port}
    connect: str | None = None  # https page that forwards to ddnet://{ip}:{port}

    def player_url(self, name: str, override: str | None = None) -> str | None:
        if override:
            return override
        if not self.player_profile:
            return None
        return self.player_profile.replace("{name}", quote(name, safe=""))

    def server_url(self, ip: str | None, port: int | None) -> str | None:
        return _fill_address(self.server, ip, port)

    def connect_url(self, ip: str | None, port: int | None) -> str | None:
        return _fill_address(self.connect, ip, port)


def _fill_address(template: str | None, ip: str | None, port: int | None) -> str | None:
    if not template or ip is None or port is None:
        return None
    return template.replace("{ip}", quote(ip, safe="")).replace("{port}", str(int(port)))


def escape_text(text: str) -> str:
    """Escape Discord markdown for normal (non-link) text. Backslash escapes work there."""
    return _LINK_TEXT_SPECIAL.sub(r"\\\1", text)


# Inside masked-link text Discord does NOT honour backslash escapes (verified in a real
# client: `\[` and `\*` render with a visible backslash) but still applies formatting.
# Balanced/unbalanced brackets and `](` render fine as-is. Formatting characters are
# therefore replaced by look-alikes instead of escaped.
_LINK_TEXT_LOOKALIKES = str.maketrans({"*": "∗", "~": "∼", "`": "ˋ", "|": "ǀ"})
# `_` only formats at word boundaries; inside words (nameless_tee) it is left untouched.
_BOUNDARY_UNDERSCORE = re.compile(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])")


def link_text(text: str) -> str:
    """Make arbitrary text safe to show as the label of a Discord masked link."""
    return _BOUNDARY_UNDERSCORE.sub("ˍ", text.translate(_LINK_TEXT_LOOKALIKES))


def _link(text: str, url: str | None) -> str:
    if not url:
        return f"**{escape_text(text)}**"
    # URLs we build are percent-encoded and user URLs are validated, so `)` cannot end the link early.
    return f"[{link_text(text)}]({url})"


def build_embed(notification: Notification, links: LinkTemplates, color: int) -> discord.Embed:
    alias = notification.seen_name if notification.seen_name not in (None, notification.player_name) else None
    # The profile link points to the name that is actually online (status.tw profiles are per name).
    profile = links.player_url(alias, None) if alias else links.player_url(
        notification.player_name, notification.profile_url_override
    )
    player = _link(notification.player_name, profile)
    if alias:
        player += f" (as {escape_text(alias)})"
    if notification.server_name:
        server = _link(notification.server_name, links.server_url(notification.server_ip, notification.server_port))
        description = f"Player {player} connected to {server}"
    else:
        description = f"Player {player} is now online"
    if notification.other_servers > 0:
        plural = "s" if notification.other_servers > 1 else ""
        description += f" (and {notification.other_servers} other server{plural})"

    embed = discord.Embed(description=description[:4096], color=color, timestamp=notification.detected_at)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


Sender = Callable[[discord.Embed], Awaitable[None]]


class NotificationQueue:
    """Serialises outgoing notifications.

    One worker sends one message at a time with a minimum spacing; discord.py additionally
    handles Discord's HTTP 429 responses (bucket-aware waiting) internally.
    """

    def __init__(
        self,
        sender: Sender,
        links: LinkTemplates,
        color: int,
        *,
        min_interval: float = 1.0,
        retry_base_delay: float = 2.0,
    ) -> None:
        self._sender = sender
        self._links = links
        self._color = color
        self._min_interval = min_interval
        self._retry_base_delay = retry_base_delay
        self._queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._task: asyncio.Task[None] | None = None
        self.sent = 0
        self.failed = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="notification-sender")

    async def enqueue(self, notification: Notification) -> None:
        try:
            self._queue.put_nowait(notification)
        except asyncio.QueueFull:
            self.failed += 1
            log.error("notification queue full, dropping", extra=fields(player=notification.player_name))

    async def drain(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except asyncio.TimeoutError:
            log.warning("pending notifications not sent before shutdown", extra=fields(pending=self._queue.qsize()))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last_sent = 0.0
        while True:
            notification = await self._queue.get()
            try:
                wait = self._min_interval - (loop.time() - last_sent)
                if wait > 0:
                    await asyncio.sleep(wait)
                await self._send_with_retry(notification)
                last_sent = loop.time()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the worker die
                self.failed += 1
                log.exception("unexpected error while sending notification")
            finally:
                self._queue.task_done()

    async def _send_with_retry(self, notification: Notification) -> None:
        embed = build_embed(notification, self._links, self._color)
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                await self._sender(embed)
            except (discord.Forbidden, discord.NotFound, ChannelUnavailable) as exc:
                self.failed += 1
                log.error(
                    "cannot post to notification channel (check DISCORD_CHANNEL_ID and bot permissions)",
                    extra=fields(player=notification.player_name, error=repr(exc)),
                )
                return
            except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
                if attempt == SEND_ATTEMPTS:
                    self.failed += 1
                    log.error(
                        "giving up sending notification",
                        extra=fields(player=notification.player_name, attempts=attempt, error=repr(exc)),
                    )
                    return
                delay = self._retry_base_delay * 2 ** (attempt - 1)
                log.warning(
                    "sending notification failed, retrying",
                    extra=fields(player=notification.player_name, attempt=attempt, delay=delay, error=repr(exc)),
                )
                await asyncio.sleep(delay)
            else:
                self.sent += 1
                log.info(
                    "notification sent",
                    extra=fields(player=notification.player_name, server=notification.server_name or "-"),
                )
                return
