"""Local status.tw test double for end-to-end tests with a real Discord server.

Serves the documented routes used by the bot (`/info`, `/player/name/{name}`, `/player/list`)
with documented response shapes, plus control routes to change presence:

    GET /_online?name=Smash[&ip=116.203.164.237&port=8303&server=...]
    GET /_offline?name=Smash
    GET /_fail?on=1        (all API routes answer HTTP 503 until /_fail?on=0)
    GET /_state

Run:   python tools/fake_statustw.py [port]        (default 8399)
Bot:   STATUS_TW_BASE_URL=http://127.0.0.1:8399
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from aiohttp import web

DEFAULT_SERVER = {"ip": "116.203.164.237", "port": 8303, "name": "[A] [KoG] GER3 #7 - Solo Gores [kog.tw]"}
NOW = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

online: dict[str, dict] = {}
failing = False


def server_dto(s: dict) -> dict:
    return {
        "ip": s["ip"], "port": s["port"], "name": s["name"],
        "numClients": 8, "numPlayers": 8, "maxClients": 64, "maxPlayers": 64,
        "hasPassword": False, "supports6": True, "supports7": False, "clientScoreKind": "time",
        "relevanceScore": 10, "logoUrl": "https://cdn.status.tw/gametypes/gores.png",
        "website": None, "discordInvite": None, "description": None, "createdAt": NOW,
        "map": {"name": "Gores_Test", "createdAt": NOW},
        "gameType": {"name": "Gores", "createdAt": NOW},
        "version": {"version": "0.6.4, 18.5", "createdAt": NOW},
        "clients": [],
    }


def client_list(name: str) -> dict:
    servers = [online[name]] if name in online else []
    return {
        "name": name,
        "createdAt": NOW,
        "players": [{
            "name": name,
            "country": {"identifier": "DE", "code": 276, "iconUrl": "https://cdn.status.tw/flags/DE.png"},
            "score": 0, "isPlayer": True, "isAfk": False, "isBot": False, "team": 0, "createdAt": NOW,
            "server": server_dto(s),
        } for s in servers],
    }


@web.middleware
async def fail_switch(request: web.Request, handler):
    if failing and not request.path.startswith("/_"):
        return web.Response(status=503, text="simulated outage")
    return await handler(request)


async def info(_: web.Request) -> web.Response:
    return web.json_response({"app": "fake-statustw", "version": "2.1.1"})


async def player(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if name.startswith("unknown"):
        return web.json_response({"statusCode": 404, "message": "Not Found"}, status=404)
    return web.json_response(client_list(name))


async def player_list(_: web.Request) -> web.Response:
    return web.json_response([client_list(n) for n in online])


async def set_online(request: web.Request) -> web.Response:
    q = request.query
    online[q["name"]] = {
        "ip": q.get("ip", DEFAULT_SERVER["ip"]),
        "port": int(q.get("port", DEFAULT_SERVER["port"])),
        "name": q.get("server", DEFAULT_SERVER["name"]),
    }
    return await state(request)


async def set_offline(request: web.Request) -> web.Response:
    online.pop(request.query["name"], None)
    return await state(request)


async def set_fail(request: web.Request) -> web.Response:
    global failing
    failing = request.query.get("on", "1") == "1"
    return await state(request)


async def state(_: web.Request) -> web.Response:
    return web.json_response({"online": online, "failing": failing})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8399
    app = web.Application(middlewares=[fail_switch])
    app.add_routes([
        web.get("/info", info),
        web.get("/player/name/{name}", player),
        web.get("/player/list", player_list),
        web.get("/_online", set_online),
        web.get("/_offline", set_offline),
        web.get("/_fail", set_fail),
        web.get("/_state", state),
    ])
    web.run_app(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
