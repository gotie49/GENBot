"""Typed views on the documented status.tw response schemas.

Only fields documented in https://api.status.tw/docs (OpenAPI 2.1.1) are read:

* ``ClientListDto``: ``name``, ``createdAt``, ``players[]``
* ``ClientWithServerDto``: ``name``, ``isPlayer``, ``isBot``, ``isAfk`` (optional), ``server``
* ``ServerDto``: ``ip``, ``port``, ``name``, ``numPlayers``, ``maxPlayers``,
  ``numClients``, ``maxClients``, ``map.name``, ``gameType.name``, ``logoUrl``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import StatusTwInvalidResponse


@dataclass(frozen=True)
class ServerInfo:
    ip: str
    port: int
    name: str
    game_type: str | None = None
    map_name: str | None = None
    num_clients: int | None = None
    max_clients: int | None = None
    logo_url: str | None = None

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass(frozen=True)
class Connection:
    """One current connection of a player to a server."""

    player_name: str
    server: ServerInfo
    is_player: bool
    is_bot: bool
    is_afk: bool | None = None


@dataclass(frozen=True)
class PlayerPresence:
    """Current presence of one exact player name (online <=> at least one connection)."""

    name: str
    connections: tuple[Connection, ...]

    @property
    def online(self) -> bool:
        return bool(self.connections)

    def connections_to(self, server_address: str | None) -> tuple[Connection, ...]:
        """Connections, optionally restricted to one ``ip:port``."""
        if server_address is None:
            return self.connections
        return tuple(c for c in self.connections if c.server.address == server_address)


def _require(obj: Any, key: str, expected: type | tuple[type, ...], context: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise StatusTwInvalidResponse(f"{context}: missing field {key!r}")
    value = obj[key]
    # bool is a subclass of int, so reject it explicitly where a number is expected.
    if not isinstance(value, expected) or (isinstance(value, bool) and expected is not bool):
        raise StatusTwInvalidResponse(f"{context}: field {key!r} has unexpected type {type(value).__name__}")
    return value


def _optional_name(obj: Any, key: str, name_key: str = "name") -> str | None:
    nested = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(nested, dict) and isinstance(nested.get(name_key), str):
        return nested[name_key]
    return None


def _optional_int(obj: dict[str, Any], key: str) -> int | None:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_server(data: Any) -> ServerInfo:
    ip = _require(data, "ip", str, "ServerDto")
    port = _require(data, "port", (int, float), "ServerDto")
    name = _require(data, "name", str, "ServerDto")
    logo = data.get("logoUrl")
    return ServerInfo(
        ip=ip,
        port=int(port),
        name=name,
        game_type=_optional_name(data, "gameType"),
        map_name=_optional_name(data, "map"),
        num_clients=_optional_int(data, "numClients"),
        max_clients=_optional_int(data, "maxClients"),
        logo_url=logo if isinstance(logo, str) and logo.startswith("https://") else None,
    )


def parse_client_list(data: Any) -> PlayerPresence:
    """Parse one ``ClientListDto``."""
    name = _require(data, "name", str, "ClientListDto")
    players = _require(data, "players", list, "ClientListDto")
    connections = []
    for entry in players:
        is_afk = entry.get("isAfk") if isinstance(entry, dict) else None
        connections.append(
            Connection(
                player_name=_require(entry, "name", str, "ClientWithServerDto"),
                server=parse_server(_require(entry, "server", dict, "ClientWithServerDto")),
                is_player=_require(entry, "isPlayer", bool, "ClientWithServerDto"),
                is_bot=_require(entry, "isBot", bool, "ClientWithServerDto"),
                is_afk=is_afk if isinstance(is_afk, bool) else None,
            )
        )
    return PlayerPresence(name=name, connections=tuple(connections))
