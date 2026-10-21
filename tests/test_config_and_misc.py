import io
import logging

import discord
import pytest

from statustw_bot.bot import minimal_intents
from statustw_bot.commands import (
    InputError,
    PlayerCommands,
    validate_player_name,
    validate_profile_url,
    validate_server_address,
)
from statustw_bot.config import ConfigError, PollStrategy, Settings, StartupStrategy, load_dotenv
from statustw_bot.logging_setup import KeyValueFormatter, RedactingFilter, fields

BASE = {"DISCORD_BOT_TOKEN": "MTIz.secret-token.value", "DISCORD_CHANNEL_ID": "123456789012345678"}


def test_defaults():
    s = Settings.from_env(BASE)
    assert s.poll_interval_seconds == 60
    assert s.poll_strategy is PollStrategy.AUTO
    assert s.startup_strategy is StartupStrategy.BASELINE
    assert s.embed_color == 0x701A75 and s.board_color == 0x5865F2
    assert s.status_tw_api_key is None
    assert s.player_profile_url_template is None and s.server_url_template is None
    assert s.public_read_commands is True
    assert s.admin_role_ids == frozenset()
    assert s.discord_status_channel_id is None and s.connect_url_template is None


def test_secrets_not_in_repr():
    s = Settings.from_env({**BASE, "STATUS_TW_API_KEY": "stw_abcdefgh"})
    assert "secret-token" not in repr(s) and "stw_abcdefgh" not in repr(s)
    assert set(s.secrets) == {"MTIz.secret-token.value", "stw_abcdefgh"}


@pytest.mark.parametrize(
    "override",
    [
        {"DISCORD_BOT_TOKEN": ""},
        {"DISCORD_CHANNEL_ID": "general"},
        {"POLL_INTERVAL_SECONDS": "5"},
        {"POLL_INTERVAL_SECONDS": "abc"},
        {"LOG_LEVEL": "LOUD"},
        {"STARTUP_STRATEGY": "maybe"},
        {"ADMIN_ROLE_IDS": "1,abc"},
        {"PLAYER_PROFILE_URL_TEMPLATE": "https://status.tw/players/"},
        {"SERVER_URL_TEMPLATE": "http://status.tw/servers/{ip}/{port}"},
        {"EMBED_COLOR": "purple"},
        {"BOARD_COLOR": "blue"},
        {"BOARD_COLOR": "#1000000"},
        {"API_REQUEST_BUDGET_PER_MINUTE": "101"},
        {"STATUS_TW_BASE_URL": "http://api.status.tw"},
        {"DISCORD_STATUS_CHANNEL_ID": "status"},
        {"DISCORD_STATUS_CHANNEL_ID": "123456789012345678"},  # same as DISCORD_CHANNEL_ID
        {"CONNECT_URL_TEMPLATE": "ddnet://{ip}:{port}"},  # Discord only links https
        {"CONNECT_URL_TEMPLATE": "https://example.org/connect/#{ip}"},
    ],
)
def test_invalid_values_rejected(override):
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, **override})


def test_full_config():
    s = Settings.from_env({
        **BASE,
        "DISCORD_GUILD_ID": "42",
        "POLL_INTERVAL_SECONDS": "120",
        "POLL_STRATEGY": "bulk",
        "STARTUP_STRATEGY": "resume",
        "ADMIN_ROLE_IDS": "1, 2,3",
        "PUBLIC_READ_COMMANDS": "false",
        "PLAYER_PROFILE_URL_TEMPLATE": "https://status.tw/players/{name}",
        "SERVER_URL_TEMPLATE": "https://status.tw/servers/{ip}/{port}",
        "EMBED_COLOR": "#123456",
        "BOARD_COLOR": "#ff8800",
        "DISCORD_STATUS_CHANNEL_ID": "99",
        "CONNECT_URL_TEMPLATE": "https://gotie49.github.io/GENBot/connect/#{ip}:{port}",
        "LOG_LEVEL": "debug",
    })
    assert s.discord_guild_id == 42 and s.poll_strategy is PollStrategy.BULK
    assert s.startup_strategy is StartupStrategy.RESUME and s.admin_role_ids == {1, 2, 3}
    assert not s.public_read_commands and s.embed_color == 0x123456 and s.log_level == "DEBUG"
    assert s.board_color == 0xFF8800
    assert s.discord_status_channel_id == 99 and s.connect_url_template.endswith("#{ip}:{port}")


def test_load_dotenv(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# comment\nDISCORD_CHANNEL_ID=42\nexport LOG_LEVEL="DEBUG"\nEMPTY=\nDISCORD_BOT_TOKEN=from-file\n')
    env = {"DISCORD_BOT_TOKEN": "from-env"}
    assert load_dotenv(str(f), env)
    assert env == {"DISCORD_BOT_TOKEN": "from-env", "DISCORD_CHANNEL_ID": "42", "LOG_LEVEL": "DEBUG", "EMPTY": ""}
    assert not load_dotenv(str(tmp_path / "missing"), env)


def test_log_redaction_and_format():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(KeyValueFormatter())
    handler.addFilter(RedactingFilter(["MTIz.secret-token.value"]))
    logger = logging.getLogger("test.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    logger.warning("token was %s", "MTIz.secret-token.value", extra=fields(player="nameless tee"))
    out = stream.getvalue()
    assert "secret-token" not in out and "***REDACTED***" in out
    assert 'player="nameless tee"' in out and "level=WARNING" in out


def test_intents_are_minimal():
    intents = minimal_intents()
    assert intents.guilds
    assert not intents.message_content and not intents.members and not intents.presences
    assert intents.value == discord.Intents(guilds=True).value


def test_validators():
    assert validate_player_name("  Smash ") == "Smash"
    for bad in ["", "   ", "x" * 33, "bad\x00name"]:
        with pytest.raises(InputError):
            validate_player_name(bad)
    assert validate_server_address("116.203.164.237:8303") == "116.203.164.237:8303"
    assert validate_server_address(None) is None
    for bad in ["kog.tw:8303", "1.2.3.4", "1.2.3.4:0", "1.2.3.4:99999", "::1:8303"]:
        with pytest.raises(InputError):
            validate_server_address(bad)
    assert validate_profile_url("https://example.org/u") == "https://example.org/u"
    for bad in ["http://x", "https://x/a)b", "https://x y"]:
        with pytest.raises(InputError):
            validate_profile_url(bad)


async def test_command_payload_is_english_and_valid():
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    payload = PlayerCommands(bot=None).to_dict(tree)  # type: ignore[arg-type]
    await client.close()
    texts = [payload["description"]]
    for sub in payload["options"]:
        texts.append(sub["description"])
        for opt in sub.get("options", []):
            texts.append(opt["description"])
            assert opt["name"] in {"player", "server", "profile_url", "aliases"}
    for text in texts:
        assert len(text) <= 100
        assert not any(ch in text for ch in "äöüÄÖÜß"), text


def test_command_group_structure():
    group = PlayerCommands(bot=None)  # type: ignore[arg-type]
    names = sorted(c.name for c in group.commands)
    assert names == ["add", "disable", "edit", "enable", "list", "remove", "status"]
    assert group.guild_only
    for name in ("remove", "enable", "disable", "status", "edit"):
        cmd = group.get_command(name)
        assert cmd._params["player"].autocomplete is not None


async def test_status_board_is_optional():
    from statustw_bot.bot import StatusBot

    without = StatusBot(Settings.from_env(BASE))
    assert without.board is None
    without.refresh_board()  # no-op, must not fail
    with_board = StatusBot(Settings.from_env({**BASE, "DISCORD_STATUS_CHANNEL_ID": "987654321098765432"}))
    assert with_board.board is not None
    for bot in (without, with_board):
        await bot.close()
