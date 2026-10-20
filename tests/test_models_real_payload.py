"""Parser check against a real `/player/list` entry captured from api.status.tw (2026-10-20)."""

from statustw_bot.statustw.models import parse_client_list

REAL_ENTRY = {
    "name": "zeus world",
    "createdAt": "2024-05-31T21:30:00.643Z",
    "players": [
        {
            "name": "zeus world",
            "country": {"identifier": "default", "code": -1, "iconUrl": "https://cdn.status.tw/flags/default.png"},
            "score": -9999,
            "isPlayer": True,
            "isAfk": True,
            "isBot": False,
            "team": 0,
            "createdAt": "2024-05-31T21:30:00.643Z",
            "skin": {"name": "giraffe"},
            "server": {
                "ip": "5.39.10.247",
                "port": 8303,
                "name": "ARX3000 Turkey - Novice",
                "numClients": 1,
                "numPlayers": 1,
                "maxClients": 64,
                "maxPlayers": 64,
                "hasPassword": False,
                "supports6": True,
                "supports7": False,
                "createdAt": "2026-08-06T12:53:03.135Z",
                "relevanceScore": -19,
                "clientScoreKind": "time",
                "logoUrl": "https://cdn.status.tw/gametypes/ddracenetwork.png",
                "map": {
                    "name": "Multeasymap",
                    "createdAt": "2024-05-31T21:03:01.195Z",
                    "sha256": "54b6587e062e72a8c2165c2cbb9e25ded0c96fc086c85e623c1fd8b695603a7a",
                    "size": 1887132,
                },
                "gameType": {"name": "DDraceNetwork", "createdAt": "2024-05-31T21:03:00.605Z"},
                "version": {"version": "0.6.4, 18.5", "createdAt": "2024-09-02T15:39:01.230Z"},
                "clients": [],
            },
        }
    ],
}


def test_real_payload_parses():
    presence = parse_client_list(REAL_ENTRY)
    assert presence.name == "zeus world" and presence.online
    conn = presence.connections[0]
    assert conn.is_afk is True and conn.is_player and not conn.is_bot
    server = conn.server
    assert server.address == "5.39.10.247:8303"
    assert server.name == "ARX3000 Turkey - Novice"
    assert server.game_type == "DDraceNetwork" and server.map_name == "Multeasymap"
    assert server.logo_url == "https://cdn.status.tw/gametypes/ddracenetwork.png"
