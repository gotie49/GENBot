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
from .storage import PlayerAlreadyExists, PlayerRecord

if TYPE_CHECKING:
    from .bot import StatusBot

log = logging.getLogger(__name__)

# Teeworlds/DDNet names are at most 15 bytes; allow some slack for odd clients.
MAX_NAME_LENGTH = 32
MAX_URL_LENGTH = 300
MAX_PLAYERS = 500
STATUS_ICON = {Status.ONLINE: "🟢", Status.OFFLINE: "⚫", Status.UNKNOWN: "❔"}


class InputError(ValueError):
    pass


def validate_player_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise InputError("Der Spielername darf nicht leer sein.")
    if len(name) > MAX_NAME_LENGTH:
        raise InputError(f"Der Spielername darf höchstens {MAX_NAME_LENGTH} Zeichen lang sein.")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise InputError("Der Spielername enthält unzulässige Steuerzeichen.")
    return name


def validate_server_address(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    host, sep, port_raw = raw.strip().rpartition(":")
    try:
        ip = ipaddress.IPv4Address(host)  # status.tw documents IPv4 server addresses
        port = int(port_raw)
    except ValueError:
        raise InputError("Server bitte als `IPv4:Port` angeben, z. B. `116.203.164.237:8303`.") from None
    if not sep or not 1 <= port <= 65535:
        raise InputError("Der Port muss zwischen 1 und 65535 liegen.")
    return f"{ip}:{port}"


def validate_profile_url(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    url = raw.strip()
    if not url.startswith("https://") or len(url) > MAX_URL_LENGTH:
        raise InputError(f"Die Profil-URL muss mit https:// beginnen und darf max. {MAX_URL_LENGTH} Zeichen haben.")
    if any(ch.isspace() or ch in "<>()[]\"'" for ch in url):
        raise InputError("Die Profil-URL enthält unzulässige Zeichen.")
    return url


def _fmt_time(value: datetime | None) -> str:
    return discord.utils.format_dt(value, "R") if value else "–"


def _describe_presence(presence: PlayerPresence, server_filter: str | None) -> str:
    if not presence.online:
        return "⚫ **offline**"
    lines = ["🟢 **online**"]
    for conn in presence.connections[:5]:
        marker = " ⭐" if server_filter and conn.server.address == server_filter else ""
        role = "Spieler" if conn.is_player else "Zuschauer"
        lines.append(f"• {escape_text(conn.server.name)} (`{conn.server.address}`, {role}){marker}")
    if len(presence.connections) > 5:
        lines.append(f"• … und {len(presence.connections) - 5} weitere")
    if server_filter and not presence.connections_to(server_filter):
        lines.append(f"_Nicht auf dem bevorzugten Server `{server_filter}` – zählt für Benachrichtigungen als offline._")
    return "\n".join(lines)


class PlayerCommands(app_commands.Group):
    def __init__(self, bot: "StatusBot") -> None:
        super().__init__(
            name="player",
            description="Teeworlds-Spieler über status.tw überwachen",
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
            "⛔ Dafür fehlt dir die Berechtigung (Administrator oder konfigurierte Admin-Rolle).", ephemeral=True
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
        if record is None:
            await interaction.response.send_message(
                f"❌ **{escape_text(name)}** ist nicht registriert. Siehe `/player list`.", ephemeral=True
            )
        return record

    async def _autocomplete_name(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        names = await self.bot.storage.search_names(current[:MAX_NAME_LENGTH])
        return [app_commands.Choice(name=n, value=n) for n in names]

    # ------------------------------------------------------------------ commands

    @app_commands.command(name="add", description="Spieler zur Überwachung hinzufügen")
    @app_commands.describe(
        spieler="Exakter Spielername (Groß-/Kleinschreibung beachten)",
        server="Optional: nur Verbindungen zu diesem Server zählen (IPv4:Port)",
        profil_url="Optional: eigene Profil-URL statt der Vorlage",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        spieler: app_commands.Range[str, 1, MAX_NAME_LENGTH],
        server: str | None = None,
        profil_url: str | None = None,
    ) -> None:
        if await self._require_manager(interaction):
            return
        try:
            name = validate_player_name(spieler)
            server_filter = validate_server_address(server)
            profile_url = validate_profile_url(profil_url)
        except InputError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if len(await self.bot.storage.list_players()) >= MAX_PLAYERS:
            await interaction.response.send_message(f"❌ Maximal {MAX_PLAYERS} Spieler.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        presence: PlayerPresence | None = None
        note = ""
        try:
            presence = await self.bot.api.get_player(name)
        except PlayerNotFound:
            await interaction.followup.send(
                f"❌ status.tw hat noch nie einen Spieler namens **{escape_text(name)}** gesehen. "
                "Bitte exakte Schreibweise prüfen.",
                ephemeral=True,
            )
            return
        except StatusTwError as exc:
            note = f"\n⚠️ status.tw ist gerade nicht erreichbar ({exc}); der Name konnte nicht geprüft werden."

        try:
            record = await self.bot.storage.add_player(
                name, added_by=interaction.user.id, profile_url=profile_url, server_filter=server_filter
            )
        except PlayerAlreadyExists:
            await interaction.followup.send(f"ℹ️ **{escape_text(name)}** wird bereits überwacht.", ephemeral=True)
            return
        self.bot.monitor.forget(record.id)
        log.info("player added", extra=fields(player=name, by=interaction.user.id, server_filter=server_filter or "-"))

        status = _describe_presence(presence, server_filter) if presence else "❔ unbekannt"
        await interaction.followup.send(
            f"✅ **{escape_text(name)}** wird jetzt überwacht.\nAktuell: {status}"
            "\n_Benachrichtigt wird beim nächsten Wechsel von offline zu online._" + note,
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="Spieler aus der Überwachung entfernen")
    @app_commands.describe(spieler="Registrierter Spielername")
    async def remove(self, interaction: discord.Interaction, spieler: app_commands.Range[str, 1, MAX_NAME_LENGTH]) -> None:
        if await self._require_manager(interaction):
            return
        record = await self._registered_player(interaction, spieler)
        if record is None:
            return
        await self.bot.storage.remove_player(record.name)
        self.bot.monitor.forget(record.id)
        log.info("player removed", extra=fields(player=record.name, by=interaction.user.id))
        await interaction.response.send_message(f"🗑️ **{escape_text(record.name)}** wurde entfernt.", ephemeral=True)

    @app_commands.command(name="enable", description="Überwachung eines Spielers aktivieren")
    @app_commands.describe(spieler="Registrierter Spielername")
    async def enable(self, interaction: discord.Interaction, spieler: app_commands.Range[str, 1, MAX_NAME_LENGTH]) -> None:
        await self._toggle(interaction, spieler, True)

    @app_commands.command(name="disable", description="Überwachung pausieren, ohne den Eintrag zu löschen")
    @app_commands.describe(spieler="Registrierter Spielername")
    async def disable(self, interaction: discord.Interaction, spieler: app_commands.Range[str, 1, MAX_NAME_LENGTH]) -> None:
        await self._toggle(interaction, spieler, False)

    async def _toggle(self, interaction: discord.Interaction, raw: str, enabled: bool) -> None:
        if await self._require_manager(interaction):
            return
        record = await self._registered_player(interaction, raw)
        if record is None:
            return
        if record.enabled == enabled:
            state = "aktiv" if enabled else "deaktiviert"
            await interaction.response.send_message(f"ℹ️ **{escape_text(record.name)}** ist bereits {state}.", ephemeral=True)
            return
        await self.bot.storage.set_enabled(record.name, enabled)
        self.bot.monitor.forget(record.id)
        log.info("player toggled", extra=fields(player=record.name, enabled=enabled, by=interaction.user.id))
        text = "▶️ Überwachung aktiviert" if enabled else "⏸️ Überwachung deaktiviert"
        await interaction.response.send_message(f"{text}: **{escape_text(record.name)}**", ephemeral=True)

    @app_commands.command(name="list", description="Alle registrierten Spieler mit letztem bekanntem Status")
    async def list_players(self, interaction: discord.Interaction) -> None:
        if await self._require_reader(interaction):
            return
        players = await self.bot.storage.list_players()
        if not players:
            await interaction.response.send_message("Noch keine Spieler registriert. Nutze `/player add`.", ephemeral=True)
            return
        lines = []
        for p in players:
            icon = STATUS_ICON[p.state.status] if p.enabled else "⏸️"
            line = f"{icon} **{escape_text(p.name)}**"
            if p.state.status is Status.ONLINE and p.state.last_server_name:
                line += f" – {escape_text(p.state.last_server_name)}"
            elif p.state.last_online_at:
                line += f" – zuletzt online {_fmt_time(p.state.last_online_at)}"
            if p.server_filter:
                line += f" · nur `{p.server_filter}`"
            if p.state.consecutive_errors:
                line += " · ⚠️ Abfrage fehlgeschlagen"
            lines.append(line)

        embed = discord.Embed(title=f"Überwachte Spieler ({len(players)})", color=self.bot.settings.embed_color)
        description = ""
        for i, line in enumerate(lines):
            if len(description) + len(line) + 40 > 4000:
                description += f"\n… und {len(lines) - i} weitere"
                break
            description += line + "\n"
        embed.description = description
        embed.set_footer(text="🟢 online · ⚫ offline · ❔ unbekannt · ⏸️ deaktiviert — Powered by status.tw")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="Aktuellen Status eines registrierten Spielers live abfragen")
    @app_commands.describe(spieler="Registrierter Spielername")
    async def status(self, interaction: discord.Interaction, spieler: app_commands.Range[str, 1, MAX_NAME_LENGTH]) -> None:
        if await self._require_reader(interaction):
            return
        record = await self._registered_player(interaction, spieler)
        if record is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            presence = await self.bot.api.get_player(record.name)
            current = _describe_presence(presence, record.server_filter)
        except PlayerNotFound:
            current = "⚫ **offline** (status.tw kennt den Namen nicht)"
        except StatusTwError as exc:
            current = f"⚠️ Live-Abfrage fehlgeschlagen: {exc}"

        embed = discord.Embed(title=record.name, color=self.bot.settings.embed_color, description=current)
        url = self.bot.links.player_url(record.name, record.profile_url)
        if url:
            embed.url = url
        embed.add_field(name="Überwachung", value="aktiv" if record.enabled else "deaktiviert")
        embed.add_field(name="Gespeicherter Status", value=record.state.status.value)
        embed.add_field(name="Zuletzt online", value=_fmt_time(record.state.last_online_at))
        if record.server_filter:
            embed.add_field(name="Bevorzugter Server", value=f"`{record.server_filter}`")
        embed.set_footer(text="Powered by status.tw")
        await interaction.followup.send(embed=embed, ephemeral=True)

    remove.autocomplete("spieler")(_autocomplete_name)
    enable.autocomplete("spieler")(_autocomplete_name)
    disable.autocomplete("spieler")(_autocomplete_name)
    status.autocomplete("spieler")(_autocomplete_name)
