"""`/player ...` application commands."""

from __future__ import annotations

import ipaddress
import logging
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .logging_setup import fields
from .notifier import escape_text
from .presence import Status
from .statustw import PlayerNotFound, PlayerPresence, StatusTwError
from .storage import NameConflict, PlayerAlreadyExists, PlayerRecord

if TYPE_CHECKING:
    from .bot import StatusBot

log = logging.getLogger(__name__)

# Teeworlds/DDNet names are at most 15 bytes; allow some slack for odd clients.
MAX_NAME_LENGTH = 32
MAX_URL_LENGTH = 300
MAX_PLAYERS = 500
MAX_ALIASES = 5
MAX_ALIASES_INPUT = 200
CLEAR_ALIASES = "-"
STATUS_ICON = {Status.ONLINE: "🟢", Status.OFFLINE: "⚫", Status.UNKNOWN: "❔"}


class InputError(ValueError):
    pass


def validate_player_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise InputError("The player name must not be empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise InputError(f"The player name must be at most {MAX_NAME_LENGTH} characters long.")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise InputError("The player name contains invalid control characters.")
    return name


def validate_server_address(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    host, sep, port_raw = raw.strip().rpartition(":")
    try:
        ip = ipaddress.IPv4Address(host)  # status.tw documents IPv4 server addresses
        port = int(port_raw)
    except ValueError:
        raise InputError("Please give the server as `IPv4:port`, e.g. `116.203.164.237:8303`.") from None
    if not sep or not 1 <= port <= 65535:
        raise InputError("The port must be between 1 and 65535.")
    return f"{ip}:{port}"


def validate_profile_url(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    url = raw.strip()
    if not url.startswith("https://") or len(url) > MAX_URL_LENGTH:
        raise InputError(f"The profile URL must start with https:// and be at most {MAX_URL_LENGTH} characters long.")
    if any(ch.isspace() or ch in "<>()[]\"'" for ch in url):
        raise InputError("The profile URL contains invalid characters.")
    return url


def parse_aliases(raw: str | None, main_name: str) -> list[str]:
    """Comma separated alias list -> validated, de-duplicated names (order kept).

    ``None``, an empty string or ``-`` mean "no aliases".
    """
    if raw is None or raw.strip() in ("", CLEAR_ALIASES):
        return []
    aliases: list[str] = []
    for part in raw.split(","):
        if not part.strip():
            continue
        try:
            alias = validate_player_name(part)
        except InputError as exc:
            raise InputError(f"Alias `{part.strip()[:MAX_NAME_LENGTH]}`: {exc}") from None
        if alias == main_name:
            raise InputError(f"`{alias}` is already the main name and cannot also be an alias.")
        if alias not in aliases:
            aliases.append(alias)
    if len(aliases) > MAX_ALIASES:
        raise InputError(f"A player can have at most {MAX_ALIASES} aliases.")
    return aliases


def _combine_presences(main_name: str, presences: list[PlayerPresence]) -> PlayerPresence:
    return PlayerPresence(main_name, tuple(c for p in presences for c in p.connections))


def _fmt_aliases(aliases: tuple[str, ...] | list[str]) -> str:
    return ", ".join(escape_text(a) for a in aliases)


def _fmt_time(value: datetime | None) -> str:
    return discord.utils.format_dt(value, "R") if value else "–"


def _describe_presence(presence: PlayerPresence, server_filter: str | None) -> str:
    if not presence.online:
        return "⚫ **offline**"
    lines = ["🟢 **online**"]
    for conn in presence.connections[:5]:
        marker = " ⭐" if server_filter and conn.server.address == server_filter else ""
        role = "playing" if conn.is_player else "spectating"
        alias = f" as {escape_text(conn.player_name)}" if conn.player_name != presence.name else ""
        lines.append(f"• {escape_text(conn.server.name)} (`{conn.server.address}`, {role}{alias}){marker}")
    if len(presence.connections) > 5:
        lines.append(f"• … and {len(presence.connections) - 5} more")
    if server_filter and not presence.connections_to(server_filter):
        lines.append(f"_Not on the preferred server `{server_filter}`, so this counts as offline for notifications._")
    return "\n".join(lines)


class PlayerCommands(app_commands.Group):
    def __init__(self, bot: "StatusBot") -> None:
        super().__init__(
            name="player",
            description="Watch Teeworlds players via status.tw",
            guild_only=True,
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        )
        self.bot = bot

    # ------------------------------------------------------------------ permissions

    def _is_manager(self, interaction: discord.Interaction) -> bool:
        settings = self.bot.settings
        user = interaction.user
        if user.id in settings.admin_user_ids:
            return True
        if isinstance(user, discord.Member):
            if user.guild_permissions.administrator:
                return True
            if settings.admin_role_ids and any(role.id in settings.admin_role_ids for role in user.roles):
                return True
        return False

    async def _deny_unless(self, interaction: discord.Interaction, allowed: bool) -> bool:
        if allowed:
            return False
        await interaction.response.send_message(
            "⛔ You are not allowed to do this (administrator or configured admin role required).", ephemeral=True
        )
        log.info("command denied", extra=fields(user=interaction.user.id, command=interaction.command.name if interaction.command else "-"))
        return True

    async def _require_manager(self, interaction: discord.Interaction) -> bool:
        return await self._deny_unless(interaction, self._is_manager(interaction))

    async def _require_reader(self, interaction: discord.Interaction) -> bool:
        return await self._deny_unless(interaction, self.bot.settings.public_read_commands or self._is_manager(interaction))

    async def _registered_player(self, interaction: discord.Interaction, raw: str) -> PlayerRecord | None:
        try:
            name = validate_player_name(raw)
        except InputError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return None
        record = await self.bot.storage.get_player(name)
        if record is None and (owner := await self.bot.storage.find_owner(name)) is not None:
            record = await self.bot.storage.get_player(owner)  # given name is an alias
        if record is None:
            await interaction.response.send_message(
                f"❌ **{escape_text(name)}** is not registered. See `/player list`.", ephemeral=True
            )
        return record

    async def _verify_names(self, names: list[str]) -> tuple[list[PlayerPresence], list[str], str]:
        """Look names up at status.tw: (presences, names never seen, warning note)."""
        presences: list[PlayerPresence] = []
        unknown: list[str] = []
        note = ""
        for name in names:
            try:
                presences.append(await self.bot.api.get_player(name))
            except PlayerNotFound:
                unknown.append(name)
            except StatusTwError as exc:
                note = f"\n⚠️ status.tw is currently unreachable ({exc}), so some names could not be verified."
        return presences, unknown, note

    async def _check_conflicts(self, names: list[str], owner: str | None) -> str | None:
        for name in names:
            current = await self.bot.storage.find_owner(name)
            if current is not None and current != owner:
                return f"❌ `{escape_text(name)}` already belongs to **{escape_text(current)}**."
        return None

    async def _autocomplete_name(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        names = await self.bot.storage.search_names(current[:MAX_NAME_LENGTH])
        return [app_commands.Choice(name=n, value=n) for n in names]

    # ------------------------------------------------------------------ commands

    @app_commands.command(name="add", description="Start watching a player")
    @app_commands.describe(
        player="Exact in-game name (case-sensitive)",
        server="Optional: only count connections to this server (IPv4:port)",
        profile_url="Optional: custom profile URL instead of the default",
        aliases=f"Optional: other in-game names of this player, comma separated (max {MAX_ALIASES})",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        player: app_commands.Range[str, 1, MAX_NAME_LENGTH],
        server: str | None = None,
        profile_url: str | None = None,
        aliases: app_commands.Range[str, 1, MAX_ALIASES_INPUT] | None = None,
    ) -> None:
        if await self._require_manager(interaction):
            return
        try:
            name = validate_player_name(player)
            server_filter = validate_server_address(server)
            custom_url = validate_profile_url(profile_url)
            alias_list = parse_aliases(aliases, name)
        except InputError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if len(await self.bot.storage.list_players()) >= MAX_PLAYERS:
            await interaction.response.send_message(f"❌ You can watch at most {MAX_PLAYERS} players.", ephemeral=True)
            return

        if await self.bot.storage.get_player(name) is not None:
            await interaction.response.send_message(f"ℹ️ **{escape_text(name)}** is already being watched.", ephemeral=True)
            return
        conflict = await self._check_conflicts([name, *alias_list], owner=None)
        if conflict:
            await interaction.response.send_message(conflict, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        presences, unknown, note = await self._verify_names([name, *alias_list])
        if unknown:
            await interaction.followup.send(
                f"❌ status.tw has never seen {_fmt_aliases(unknown)}. Please check the exact spelling "
                "(names are case-sensitive).",
                ephemeral=True,
            )
            return

        try:
            record = await self.bot.storage.add_player(
                name, added_by=interaction.user.id, profile_url=custom_url, server_filter=server_filter
            )
            if alias_list:
                await self.bot.storage.set_aliases(record.id, alias_list)
        except PlayerAlreadyExists:
            await interaction.followup.send(f"ℹ️ **{escape_text(name)}** is already being watched.", ephemeral=True)
            return
        except NameConflict as exc:
            await self.bot.storage.remove_player(name)
            await interaction.followup.send(
                f"❌ `{escape_text(exc.name)}` already belongs to **{escape_text(exc.owner)}**.", ephemeral=True
            )
            return
        self.bot.monitor.forget(record.id)
        self.bot.refresh_board()
        log.info("player added", extra=fields(player=name, aliases=",".join(alias_list) or "-",
                                              by=interaction.user.id, server_filter=server_filter or "-"))

        status = (
            _describe_presence(_combine_presences(name, presences), server_filter)
            if len(presences) == 1 + len(alias_list) else "❔ unknown"
        )
        alias_text = f"\nAliases: {_fmt_aliases(alias_list)}" if alias_list else ""
        await interaction.followup.send(
            f"✅ Now watching **{escape_text(name)}**.{alias_text}\nCurrently: {status}"
            "\n_A notification is sent the next time they go from offline to online._" + note,
            ephemeral=True,
        )

    @app_commands.command(name="edit", description="Replace the aliases of a watched player")
    @app_commands.describe(
        player="Name of a watched player",
        aliases=f"Other in-game names, comma separated (max {MAX_ALIASES}); `-` removes all aliases",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        player: app_commands.Range[str, 1, MAX_NAME_LENGTH],
        aliases: app_commands.Range[str, 1, MAX_ALIASES_INPUT],
    ) -> None:
        if await self._require_manager(interaction):
            return
        record = await self._registered_player(interaction, player)
        if record is None:
            return
        try:
            alias_list = parse_aliases(aliases, record.name)
        except InputError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        conflict = await self._check_conflicts(alias_list, owner=record.name)
        if conflict:
            await interaction.response.send_message(conflict, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        new_names = [a for a in alias_list if a not in record.aliases]
        _, unknown, note = await self._verify_names(new_names)
        if unknown:
            await interaction.followup.send(
                f"❌ status.tw has never seen {_fmt_aliases(unknown)}. Please check the exact spelling "
                "(names are case-sensitive).",
                ephemeral=True,
            )
            return
        try:
            await self.bot.storage.set_aliases(record.id, alias_list)
        except NameConflict as exc:
            await interaction.followup.send(
                f"❌ `{escape_text(exc.name)}` already belongs to **{escape_text(exc.owner)}**.", ephemeral=True
            )
            return
        # The set of names changed: treat the next result as a fresh baseline (no surprise notification).
        self.bot.monitor.forget(record.id)
        self.bot.refresh_board()
        log.info("player aliases changed", extra=fields(player=record.name, aliases=",".join(alias_list) or "-",
                                                        by=interaction.user.id))
        text = f"Aliases: {_fmt_aliases(alias_list)}" if alias_list else "No aliases."
        await interaction.followup.send(f"✏️ Updated **{escape_text(record.name)}**. {text}" + note, ephemeral=True)

    @app_commands.command(name="remove", description="Stop watching a player and delete the entry")
    @app_commands.describe(player="Name of a watched player")
    async def remove(self, interaction: discord.Interaction, player: app_commands.Range[str, 1, MAX_NAME_LENGTH]) -> None:
        if await self._require_manager(interaction):
            return
        record = await self._registered_player(interaction, player)
        if record is None:
            return
        await self.bot.storage.remove_player(record.name)
        self.bot.monitor.forget(record.id)
        self.bot.refresh_board()
        log.info("player removed", extra=fields(player=record.name, by=interaction.user.id))
        await interaction.response.send_message(f"🗑️ Removed **{escape_text(record.name)}**.", ephemeral=True)

    @app_commands.command(name="enable", description="Resume watching a player")
    @app_commands.describe(player="Name of a watched player")
    async def enable(self, interaction: discord.Interaction, player: app_commands.Range[str, 1, MAX_NAME_LENGTH]) -> None:
        await self._toggle(interaction, player, True)

    @app_commands.command(name="disable", description="Pause watching a player without deleting the entry")
    @app_commands.describe(player="Name of a watched player")
    async def disable(self, interaction: discord.Interaction, player: app_commands.Range[str, 1, MAX_NAME_LENGTH]) -> None:
        await self._toggle(interaction, player, False)

    async def _toggle(self, interaction: discord.Interaction, raw: str, enabled: bool) -> None:
        if await self._require_manager(interaction):
            return
        record = await self._registered_player(interaction, raw)
        if record is None:
            return
        if record.enabled == enabled:
            state = "enabled" if enabled else "disabled"
            await interaction.response.send_message(f"ℹ️ **{escape_text(record.name)}** is already {state}.", ephemeral=True)
            return
        await self.bot.storage.set_enabled(record.name, enabled)
        self.bot.monitor.forget(record.id)
        self.bot.refresh_board()
        log.info("player toggled", extra=fields(player=record.name, enabled=enabled, by=interaction.user.id))
        text = "▶️ Resumed watching" if enabled else "⏸️ Paused watching"
        await interaction.response.send_message(f"{text}: **{escape_text(record.name)}**", ephemeral=True)

    @app_commands.command(name="list", description="List all watched players with their last known status")
    async def list_players(self, interaction: discord.Interaction) -> None:
        if await self._require_reader(interaction):
            return
        players = await self.bot.storage.list_players()
        if not players:
            await interaction.response.send_message("No players registered yet. Use `/player add`.", ephemeral=True)
            return
        lines = []
        for p in players:
            icon = STATUS_ICON[p.state.status] if p.enabled else "⏸️"
            line = f"{icon} **{escape_text(p.name)}**"
            if p.aliases:
                line += f" (aka {_fmt_aliases(p.aliases)})"
            if p.state.status is Status.ONLINE and p.state.last_server_name:
                line += f" – {escape_text(p.state.last_server_name)}"
            elif p.state.last_online_at:
                line += f" – last online {_fmt_time(p.state.last_online_at)}"
            if p.server_filter:
                line += f" · only `{p.server_filter}`"
            if p.state.consecutive_errors:
                line += " · ⚠️ last lookup failed"
            lines.append(line)

        embed = discord.Embed(title=f"Watched players ({len(players)})", color=self.bot.settings.embed_color)
        description = ""
        for i, line in enumerate(lines):
            if len(description) + len(line) + 40 > 4000:
                description += f"\n… and {len(lines) - i} more"
                break
            description += line + "\n"
        embed.description = description
        embed.set_footer(text="🟢 online · ⚫ offline · ❔ unknown · ⏸️ paused — Powered by status.tw")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="Look up the live status of a watched player")
    @app_commands.describe(player="Name of a watched player")
    async def status(self, interaction: discord.Interaction, player: app_commands.Range[str, 1, MAX_NAME_LENGTH]) -> None:
        if await self._require_reader(interaction):
            return
        record = await self._registered_player(interaction, player)
        if record is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        presences, _, note = await self._verify_names(list(record.names))
        combined = _combine_presences(record.name, presences)
        if note and not combined.online:
            current = "⚠️ Live lookup failed, status unknown."
        else:
            current = _describe_presence(combined, record.server_filter)

        embed = discord.Embed(title=record.name, color=self.bot.settings.embed_color, description=current)
        url = self.bot.links.player_url(record.name, record.profile_url)
        if url:
            embed.url = url
        if record.aliases:
            embed.add_field(name="Aliases", value=_fmt_aliases(record.aliases), inline=False)
        embed.add_field(name="Watching", value="active" if record.enabled else "paused")
        embed.add_field(name="Stored status", value=record.state.status.value)
        embed.add_field(name="Last online", value=_fmt_time(record.state.last_online_at))
        if record.server_filter:
            embed.add_field(name="Preferred server", value=f"`{record.server_filter}`")
        embed.set_footer(text="Powered by status.tw")
        await interaction.followup.send(embed=embed, ephemeral=True)

    remove.autocomplete("player")(_autocomplete_name)
    enable.autocomplete("player")(_autocomplete_name)
    disable.autocomplete("player")(_autocomplete_name)
    status.autocomplete("player")(_autocomplete_name)
    edit.autocomplete("player")(_autocomplete_name)
