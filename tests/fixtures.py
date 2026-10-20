"""Sample payloads shaped exactly like the documented status.tw schemas (ClientListDto etc.)."""

from __future__ import annotations

from typing import Any


def server_dto(ip: str = "116.203.164.237", port: int = 8303, name: str = "[A] [KoG] GER3 #7 - Solo Gores [kog.tw]") -> dict[str, Any]:
    return {
        "ip": ip,
        "port": port,
        "name": name,
        "numClients": 10,
        "numPlayers": 9,
        "maxClients": 64,
        "maxPlayers": 64,
        "hasPassword": False,
        "supports6": True,
        "supports7": False,
        "clientScoreKind": "time",
        "relevanceScore": 12,
        "logoUrl": "https://cdn.status.tw/gametypes/gores.png",
        "website": None,
        "discordInvite": None,
        "description": None,
        "createdAt": "2024-05-31T21:03:00.605Z",
        "map": {"name": "Gores_Easy", "createdAt": "2024-05-31T21:03:00.605Z"},
        "gameType": {"name": "Gores", "createdAt": "2024-05-31T21:03:00.605Z"},
        "version": {"version": "0.6.4, 18.5", "createdAt": "2024-05-31T21:03:00.605Z"},
        "clients": [],
    }


def client_list_dto(name: str = "Smash", servers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    servers = [] if servers is None else servers
    return {
        "name": name,
        "createdAt": "2024-05-31T21:30:00.643Z",
        "players": [
            {
                "name": name,
                "country": {"identifier": "DE", "code": 276, "iconUrl": "https://cdn.status.tw/flags/DE.png"},
                "score": 0,
                "isPlayer": True,
                "isAfk": False,
                "isBot": False,
                "team": 0,
                "createdAt": "2024-05-31T21:30:00.643Z",
                "server": server,
            }
            for server in servers
        ],
    }
