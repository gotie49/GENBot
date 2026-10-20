"""Discord client: wires storage, status.tw client, monitor, notifier and commands together."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from .commands import PlayerCommands
from .config import Settings
from .logging_setup import fields
from .monitor import MonitorOptions, PresenceMonitor
from .notifier import ChannelUnavailable, LinkTemplates, NotificationQueue
from .statustw import RequestBudget, StatusTwClient, StatusTwError
from .storage import Storage

log = logging.getLogger(__name__)

SHUTDOWN_DRAIN_SECONDS = 10.0


def minimal_intents() -> discord.Intents:
    # Slash commands arrive via interactions and need no privileged intents.
    # `guilds` (non-privileged) keeps discord.py's guild/channel cache working.
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


class StatusBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            intents=minimal_intents(),
            allowed_mentions=discord.AllowedMentions.none(),
            activity=discord.Activity(type=discord.ActivityType.watching, name="status.tw"),
        )
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(settings.database_path)
        self.api = StatusTwClient(
            base_url=settings.status_tw_base_url,
            api_key=settings.status_tw_api_key,
            timeout_seconds=settings.api_timeout_seconds,
            max_retries=settings.api_max_retries,
            budget=RequestBudget(settings.api_request_budget_per_minute),
        )
        self.links = LinkTemplates(
            player_profile=settings.player_profile_url_template,
            server=settings.server_url_template,
        )
        self.notifications = NotificationQueue(
            self._send_to_channel,
            self.links,
            settings.embed_color,
            min_interval=settings.notify_min_interval_seconds,
        )
        self.monitor = PresenceMonitor(
            self.storage,
            self.api,
            self.notifications.enqueue,
            MonitorOptions(
                poll_interval_seconds=settings.poll_interval_seconds,
                poll_strategy=settings.poll_strategy,
                startup_strategy=settings.startup_strategy,
                offline_confirmations=settings.offline_confirmations,
                notify_cooldown_seconds=settings.notify_cooldown_seconds,
                heartbeat_file=settings.heartbeat_file,
            ),
        )
        self._channel: discord.abc.Messageable | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._closing = False

    # ------------------------------------------------------------------ lifecycle

    async def setup_hook(self) -> None:
        await self.storage.open()
        players = await self.storage.list_players()
        log.info(
            "database ready",
            extra=fields(path=self.settings.database_path, players=len(players),
                         enabled=sum(p.enabled for p in players)),
        )
        for p in players:
            log.info("registered player", extra=fields(player=p.name, enabled=p.enabled,
                                                       status=p.state.status.value,
                                                       server_filter=p.server_filter or "-"))

        await self.api.start()
        try:
            info = await self.api.get_info()
            log.info("status.tw API reachable", extra=fields(app=info.get("app"), version=info.get("version"),
                                                              authenticated=bool(self.settings.status_tw_api_key)))
        except StatusTwError as exc:
            # Not fatal: the monitor treats failures as "unknown" and keeps retrying.
            log.warning("status.tw API not reachable at startup", extra=fields(error=str(exc)))

        self.tree.add_command(PlayerCommands(self))
        self.tree.on_error = self._on_app_command_error
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("slash commands synced to guild", extra=fields(guild=guild.id, commands=len(synced)))
        else:
            synced = await self.tree.sync()
            log.info("slash commands synced globally (may take up to an hour to appear)",
                     extra=fields(commands=len(synced)))

        self.notifications.start()
        self._monitor_task = asyncio.create_task(self.monitor.run_forever(), name="presence-monitor")
        self._monitor_task.add_done_callback(self._on_monitor_done)

    async def on_ready(self) -> None:
        log.info("connected to Discord", extra=fields(user=str(self.user), user_id=self.user.id if self.user else "-",
                                                      guilds=len(self.guilds)))
        await self._resolve_channel()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("shutting down")
        self.monitor.stop()
        if self._monitor_task is not None:
            try:
                await asyncio.wait_for(self._monitor_task, timeout=SHUTDOWN_DRAIN_SECONDS)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._monitor_task.cancel()
            except Exception:
                pass  # already logged by the done callback
        await self.notifications.drain(SHUTDOWN_DRAIN_SECONDS)
        await self.notifications.stop()
        await self.api.close()
        await self.storage.close()
        await super().close()
        log.info("shutdown complete")

    def _on_monitor_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._closing:
            return
        exc = task.exception()
        log.critical("presence monitor stopped unexpectedly; shutting down so the container restarts",
                     exc_info=exc)
        asyncio.get_running_loop().create_task(self.close())

    # ------------------------------------------------------------------ helpers

    async def _resolve_channel(self) -> discord.abc.Messageable | None:
        if self._channel is not None:
            return self._channel
        channel_id = self.settings.discord_channel_id
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                log.error("notification channel not accessible; check DISCORD_CHANNEL_ID and bot permissions",
                          extra=fields(channel=channel_id, status=exc.status))
                return None
        if not isinstance(channel, discord.abc.Messageable):
            log.error("DISCORD_CHANNEL_ID is not a text channel", extra=fields(channel=channel_id))
            return None
        if isinstance(channel, discord.abc.GuildChannel):
            perms = channel.permissions_for(channel.guild.me)
            missing = [n for n, ok in (("view_channel", perms.view_channel), ("send_messages", perms.send_messages),
                                       ("embed_links", perms.embed_links)) if not ok]
            if missing:
                log.error("bot lacks permissions in notification channel", extra=fields(channel=channel_id,
                                                                                     missing=",".join(missing)))
            else:
                log.info("notification channel ready", extra=fields(channel=channel_id, name=channel.name,
                                                                    guild=channel.guild.name))
        self._channel = channel
        return channel

    async def _send_to_channel(self, embed: discord.Embed) -> None:
        await self.wait_until_ready()
        channel = await self._resolve_channel()
        if channel is None:
            raise ChannelUnavailable(self.settings.discord_channel_id)
        await channel.send(embed=embed)

    async def _on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("slash command failed", exc_info=error,
                  extra=fields(command=interaction.command.qualified_name if interaction.command else "-",
                               user=interaction.user.id))
        message = "❌ Interner Fehler bei der Ausführung des Befehls. Details stehen im Log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

