"""Central configuration, read exclusively from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, MutableMapping


class ConfigError(ValueError):
    """Raised when the environment does not contain a valid configuration."""


class StartupStrategy(str, Enum):
    """How the first successful observation of a player after (re)start is treated."""

    # First observation only records a baseline; never notifies. (Default)
    BASELINE = "baseline"
    # Compare with the persisted state: offline -> online across a restart notifies,
    # a player that was online before the restart and still is does not.
    RESUME = "resume"
    # Notify for every player that is online at startup, unless the persisted state
    # shows the current online session has already been announced.
    NOTIFY = "notify"


class PollStrategy(str, Enum):
    """Which documented status.tw endpoint is used to determine presence."""

    # One `GET /player/name/{name}` per enabled player (small responses).
    PER_PLAYER = "per_player"
    # One `GET /player/list` per cycle (one request, but ~5 MB response).
    BULK = "bulk"
    # per_player while it fits into the request budget, otherwise bulk.
    AUTO = "auto"


def load_dotenv(path: str = ".env", env: MutableMapping[str, str] | None = None) -> bool:
    """Load ``KEY=VALUE`` lines from a local file for non-Docker runs.

    Variables already present in the environment always win. Returns True if the file existed.
    """
    target = os.environ if env is None else env
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in target:
            target[key] = value
    return True


def _get_str(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _get_int(env: Mapping[str, str], key: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _get_str(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _get_float(env: Mapping[str, str], key: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = _get_str(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get_str(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean (true/false), got {raw!r}")


def _get_id_list(env: Mapping[str, str], key: str) -> frozenset[int]:
    raw = _get_str(env, key)
    if raw is None:
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ConfigError(f"{key} must be a comma separated list of numeric Discord IDs, got {part!r}")
        ids.add(int(part))
    return frozenset(ids)


def _get_enum(env: Mapping[str, str], key: str, enum_type: type[Enum], default: Enum) -> Enum:
    raw = _get_str(env, key)
    if raw is None:
        return default
    try:
        return enum_type(raw.lower())
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)  # type: ignore[attr-defined]
        raise ConfigError(f"{key} must be one of: {allowed}; got {raw!r}") from exc


def _get_url_template(env: Mapping[str, str], key: str, placeholders: tuple[str, ...]) -> str | None:
    template = _get_str(env, key)
    if template is None:
        return None
    if not template.startswith("https://"):
        raise ConfigError(f"{key} must start with https://")
    for placeholder in placeholders:
        if "{" + placeholder + "}" not in template:
            raise ConfigError(f"{key} must contain the placeholder {{{placeholder}}}")
    return template


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str = field(repr=False)
    discord_channel_id: int
    discord_guild_id: int | None
    discord_status_channel_id: int | None
    status_tw_api_key: str | None = field(repr=False)
    status_tw_base_url: str
    poll_interval_seconds: int
    poll_strategy: PollStrategy
    api_request_budget_per_minute: int
    api_timeout_seconds: float
    api_max_retries: int
    startup_strategy: StartupStrategy
    offline_confirmations: int
    notify_cooldown_seconds: int
    notify_min_interval_seconds: float
    database_path: str
    log_level: str
    admin_role_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    public_read_commands: bool
    player_profile_url_template: str | None
    server_url_template: str | None
    connect_url_template: str | None
    embed_color: int
    heartbeat_file: str | None

    @property
    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in log output."""
        return tuple(s for s in (self.discord_bot_token, self.status_tw_api_key) if s)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        token = _get_str(env, "DISCORD_BOT_TOKEN")
        if not token:
            raise ConfigError("DISCORD_BOT_TOKEN is required")

        channel_raw = _get_str(env, "DISCORD_CHANNEL_ID")
        if not channel_raw or not channel_raw.isdigit():
            raise ConfigError("DISCORD_CHANNEL_ID is required and must be a numeric Discord channel ID")

        guild_raw = _get_str(env, "DISCORD_GUILD_ID")
        if guild_raw is not None and not guild_raw.isdigit():
            raise ConfigError("DISCORD_GUILD_ID must be a numeric Discord server (guild) ID")

        status_channel_raw = _get_str(env, "DISCORD_STATUS_CHANNEL_ID")
        if status_channel_raw is not None and not status_channel_raw.isdigit():
            raise ConfigError("DISCORD_STATUS_CHANNEL_ID must be a numeric Discord channel ID")
        if status_channel_raw is not None and status_channel_raw == channel_raw:
            raise ConfigError("DISCORD_STATUS_CHANNEL_ID must differ from DISCORD_CHANNEL_ID")

        log_level = (_get_str(env, "LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL; got {log_level!r}")

        color_raw = _get_str(env, "EMBED_COLOR", "#701A75") or "#701A75"
        try:
            embed_color = int(color_raw.lstrip("#"), 16)
        except ValueError as exc:
            raise ConfigError(f"EMBED_COLOR must be a hex color like #701A75, got {color_raw!r}") from exc
        if not 0 <= embed_color <= 0xFFFFFF:
            raise ConfigError("EMBED_COLOR must be within #000000..#FFFFFF")

        base_url = (_get_str(env, "STATUS_TW_BASE_URL", "https://api.status.tw") or "").rstrip("/")
        # Plain http is only accepted for a local test double (tools/fake_statustw.py).
        is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
        if not base_url.startswith("https://") and not is_local:
            raise ConfigError("STATUS_TW_BASE_URL must start with https:// (http only for 127.0.0.1/localhost)")

        return cls(
            discord_bot_token=token,
            discord_channel_id=int(channel_raw),
            discord_guild_id=int(guild_raw) if guild_raw else None,
            discord_status_channel_id=int(status_channel_raw) if status_channel_raw else None,
            status_tw_api_key=_get_str(env, "STATUS_TW_API_KEY"),
            status_tw_base_url=base_url,
            poll_interval_seconds=_get_int(env, "POLL_INTERVAL_SECONDS", 60, minimum=15, maximum=3600),
            poll_strategy=_get_enum(env, "POLL_STRATEGY", PollStrategy, PollStrategy.AUTO),  # type: ignore[arg-type]
            # status.tw documents 100 requests / 60 s per IP; keep head room by default.
            api_request_budget_per_minute=_get_int(env, "API_REQUEST_BUDGET_PER_MINUTE", 60, minimum=1, maximum=100),
            api_timeout_seconds=_get_float(env, "API_TIMEOUT_SECONDS", 15.0, minimum=1.0, maximum=120.0),
            api_max_retries=_get_int(env, "API_MAX_RETRIES", 2, minimum=0, maximum=5),
            startup_strategy=_get_enum(env, "STARTUP_STRATEGY", StartupStrategy, StartupStrategy.BASELINE),  # type: ignore[arg-type]
            offline_confirmations=_get_int(env, "OFFLINE_CONFIRMATIONS", 2, minimum=1, maximum=20),
            notify_cooldown_seconds=_get_int(env, "NOTIFY_COOLDOWN_SECONDS", 0, minimum=0, maximum=86400),
            notify_min_interval_seconds=_get_float(env, "NOTIFY_MIN_INTERVAL_SECONDS", 1.0, minimum=0.0, maximum=60.0),
            database_path=_get_str(env, "DATABASE_PATH", "data/bot.sqlite3") or "data/bot.sqlite3",
            log_level=log_level,
            admin_role_ids=_get_id_list(env, "ADMIN_ROLE_IDS"),
            admin_user_ids=_get_id_list(env, "ADMIN_USER_IDS"),
            public_read_commands=_get_bool(env, "PUBLIC_READ_COMMANDS", True),
            player_profile_url_template=_get_url_template(env, "PLAYER_PROFILE_URL_TEMPLATE", ("name",)),
            server_url_template=_get_url_template(env, "SERVER_URL_TEMPLATE", ("ip", "port")),
            connect_url_template=_get_url_template(env, "CONNECT_URL_TEMPLATE", ("ip", "port")),
            embed_color=embed_color,
            heartbeat_file=_get_str(env, "HEARTBEAT_FILE"),
        )
