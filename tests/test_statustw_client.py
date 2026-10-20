import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from fixtures import client_list_dto, server_dto
from statustw_bot.statustw import (
    PlayerNotFound,
    RequestBudget,
    StatusTwClient,
    StatusTwInvalidResponse,
    StatusTwRateLimited,
    StatusTwUnavailable,
)
from statustw_bot.statustw import client as client_module


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(client_module, "BACKOFF_BASE_SECONDS", 0.01)


class FakeApi:
    """Tiny stand-in for api.status.tw following the documented routes."""

    def __init__(self) -> None:
        self.handlers = {}
        self.calls: list[str] = []
        self.auth_headers: list[str | None] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/player/name/{name}", self._route)
        app.router.add_get("/player/list", self._route)
        app.router.add_get("/info", self._route)
        return app

    async def _route(self, request: web.Request) -> web.StreamResponse:
        key = request.path if "name" not in request.match_info else "/player/name/" + request.match_info["name"]
        self.calls.append(key)
        self.auth_headers.append(request.headers.get("Authorization"))
        handler = self.handlers[key]
        result = handler(request) if callable(handler) else handler.pop(0)
        if asyncio.iscoroutine(result):
            result = await result
        return result


@pytest.fixture
async def api():
    fake = FakeApi()
    server = TestServer(fake.app())
    await server.start_server()
    client = StatusTwClient(
        base_url=str(server.make_url("")).rstrip("/"),
        api_key="stw_secret_token",
        timeout_seconds=0.5,
        max_retries=2,
        budget=RequestBudget(100),
    )
    await client.start()
    yield fake, client
    await client.close()
    await server.close()


async def test_online_player_is_parsed(api):
    fake, client = api
    fake.handlers["/player/name/Smash"] = lambda r: web.json_response(client_list_dto("Smash", [server_dto()]))
    presence = await client.get_player("Smash")
    assert presence.online
    conn = presence.connections[0]
    assert conn.server.name == "[A] [KoG] GER3 #7 - Solo Gores [kog.tw]"
    assert conn.server.address == "116.203.164.237:8303"
    assert conn.server.game_type == "Gores"
    assert fake.auth_headers == ["Bearer stw_secret_token"]


async def test_names_with_spaces_and_slashes_are_encoded(api):
    fake, client = api
    seen = []

    def handler(request):
        seen.append((request.match_info["name"], request.query.get("withoutClients")))
        return web.json_response(client_list_dto("nameless/ tee"))

    fake.handlers["/player/name/nameless/ tee"] = handler
    presence = await client.get_player("nameless/ tee")
    assert not presence.online
    assert seen == [("nameless/ tee", "true")]


async def test_404_raises_player_not_found(api):
    fake, client = api
    fake.handlers["/player/name/ghost"] = lambda r: web.json_response({"statusCode": 404}, status=404)
    with pytest.raises(PlayerNotFound):
        await client.get_player("ghost")
    assert len(fake.calls) == 1  # not retried


async def test_5xx_is_retried_then_succeeds(api):
    fake, client = api
    fake.handlers["/player/name/Smash"] = [
        web.Response(status=502),
        web.Response(status=503),
        web.json_response(client_list_dto("Smash")),
    ]
    presence = await client.get_player("Smash")
    assert not presence.online
    assert len(fake.calls) == 3


async def test_5xx_after_retries_raises_unavailable(api):
    fake, client = api
    fake.handlers["/player/name/Smash"] = lambda r: web.Response(status=500)
    with pytest.raises(StatusTwUnavailable):
        await client.get_player("Smash")
    assert len(fake.calls) == 3  # 1 + max_retries


async def test_timeout_raises_unavailable(api):
    fake, client = api

    async def slow(request):
        await asyncio.sleep(2)
        return web.json_response(client_list_dto("Smash"))

    fake.handlers["/player/name/Smash"] = slow
    with pytest.raises(StatusTwUnavailable):
        await client.get_player("Smash")


async def test_invalid_json_and_schema_raise_invalid_response(api):
    fake, client = api
    fake.handlers["/player/name/a"] = lambda r: web.Response(text="<html>oops</html>", content_type="text/html")
    fake.handlers["/player/name/b"] = lambda r: web.json_response({"name": "b"})  # missing `players`
    fake.handlers["/player/name/c"] = lambda r: web.json_response(
        {"name": "c", "createdAt": "x", "players": [{"name": "c", "isPlayer": True, "isBot": False}]}
    )  # connection without `server`
    for name in "abc":
        with pytest.raises(StatusTwInvalidResponse):
            await client.get_player(name)


async def test_400_is_not_retried(api):
    fake, client = api
    fake.handlers["/player/name/x"] = lambda r: web.json_response({"message": "bad"}, status=400)
    with pytest.raises(StatusTwInvalidResponse):
        await client.get_player("x")
    assert len(fake.calls) == 1


async def test_429_pauses_budget_and_retries(api):
    fake, client = api
    fake.handlers["/player/name/Smash"] = [
        web.Response(status=429, headers={"x-ratelimit-reset": "0.05"}),
        web.json_response(client_list_dto("Smash", [server_dto()])),
    ]
    presence = await client.get_player("Smash")
    assert presence.online and len(fake.calls) == 2


async def test_429_exhausted_raises_rate_limited(api):
    fake, client = api
    fake.handlers["/player/name/Smash"] = lambda r: web.Response(status=429, headers={"x-ratelimit-reset": "0.01"})
    with pytest.raises(StatusTwRateLimited):
        await client.get_player("Smash")


async def test_bulk_list_maps_exact_names(api):
    fake, client = api
    fake.handlers["/player/list"] = lambda r: web.json_response(
        [client_list_dto("Smash", [server_dto()]), client_list_dto("Smasher", [server_dto(port=8304)])]
    )
    online = await client.list_online_players()
    assert set(online) == {"Smash", "Smasher"}
    assert online["Smasher"].connections[0].server.port == 8304


async def test_bulk_list_rejects_non_array(api):
    fake, client = api
    fake.handlers["/player/list"] = lambda r: web.json_response({"oops": True})
    with pytest.raises(StatusTwInvalidResponse):
        await client.list_online_players()


async def test_quota_header_blocks_budget(api):
    fake, client = api
    fake.handlers["/info"] = lambda r: web.json_response(
        {"app": "statustw-api", "version": "2.1.1"}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "30"}
    )
    await client.get_info()
    assert client.budget.wait_time() > 25


async def test_request_budget_sliding_window():
    now = [0.0]
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    budget = RequestBudget(2, clock=lambda: now[0], sleep=fake_sleep)
    await budget.acquire()
    await budget.acquire()
    await budget.acquire()  # third must wait for the 60 s window
    assert slept == [60.0]
