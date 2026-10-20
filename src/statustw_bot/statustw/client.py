"""Async client for the documented status.tw REST API (https://api.status.tw/docs).

Endpoints used (all ``GET``, OpenAPI 2.1.1):

* ``/info``                 – service name/version (startup connectivity check)
* ``/player/name/{name}``   – exact player name -> current connections; 404 = never seen
* ``/player/list``          – all currently online players (bulk mode, large response)
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

from .. import __version__
from ..logging_setup import fields
from .errors import (
    PlayerNotFound,
    StatusTwInvalidResponse,
    StatusTwRateLimited,
    StatusTwUnavailable,
)
from .models import PlayerPresence, parse_client_list
from .rate_limit import RequestBudget

log = logging.getLogger(__name__)

USER_AGENT = f"statustw-discord-bot/{__version__} (Discord presence bot)"
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
# Bulk responses are several MB; give them more time than single lookups.
BULK_TIMEOUT_FACTOR = 3


class StatusTwClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.status.tw",
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        budget: RequestBudget | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self.budget = budget or RequestBudget(60)
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "StatusTwClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None:
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            if self._api_key:
                # Documented scheme: `Authorization: Bearer stw_...`
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
            self._owns_session = True

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ endpoints

    async def get_info(self) -> dict[str, Any]:
        data = await self._get_json("/info")
        if not isinstance(data, dict) or not isinstance(data.get("version"), str):
            raise StatusTwInvalidResponse("/info: unexpected response")
        return data

    async def get_player(self, name: str) -> PlayerPresence:
        """Presence of an exact (case-sensitive) player name.

        Raises :class:`PlayerNotFound` if status.tw has never seen the name.
        """
        path = "/player/name/" + quote(name, safe="")
        data = await self._get_json(path, params={"withoutClients": "true"}, not_found_name=name)
        presence = parse_client_list(data)
        if presence.name != name:
            raise StatusTwInvalidResponse(f"/player/name: asked for {name!r}, got {presence.name!r}")
        return presence

    async def list_online_players(self) -> dict[str, PlayerPresence]:
        """All currently online players, keyed by exact name."""
        data = await self._get_json("/player/list", timeout_factor=BULK_TIMEOUT_FACTOR)
        if not isinstance(data, list):
            raise StatusTwInvalidResponse("/player/list: expected a JSON array")
        result: dict[str, PlayerPresence] = {}
        for entry in data:
            presence = parse_client_list(entry)
            result[presence.name] = presence
        return result

    # ------------------------------------------------------------------ transport

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        not_found_name: str | None = None,
        timeout_factor: float = 1,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("StatusTwClient.start() must be called first")
        url = URL(self._base_url + path, encoded=True)
        timeout = aiohttp.ClientTimeout(total=self._timeout * timeout_factor)
        attempt = 0
        while True:
            await self.budget.acquire()
            try:
                async with self._session.get(url, params=params, timeout=timeout) as resp:
                    self._observe_rate_headers(resp)
                    if resp.status == 200:
                        try:
                            return await resp.json(content_type=None)
                        except (ValueError, aiohttp.ContentTypeError) as exc:
                            raise StatusTwInvalidResponse(f"{path}: response is not valid JSON") from exc
                    if resp.status == 404 and not_found_name is not None:
                        raise PlayerNotFound(not_found_name)
                    if resp.status == 429:
                        retry_after = self._retry_after(resp)
                        self.budget.block_for(retry_after)
                        log.warning(
                            "status.tw rate limit hit",
                            extra=fields(path=_safe_path(path), retry_after=retry_after, attempt=attempt),
                        )
                        if attempt >= self._max_retries:
                            raise StatusTwRateLimited(retry_after)
                        attempt += 1
                        continue  # budget.acquire() waits for the pause
                    if resp.status >= 500:
                        error: Exception = StatusTwUnavailable(f"{path}: HTTP {resp.status}")
                    else:
                        # 400 / 401 / 403 / 410 / ...: not retryable.
                        raise StatusTwInvalidResponse(f"{_safe_path(path)}: HTTP {resp.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                error = StatusTwUnavailable(f"{_safe_path(path)}: {type(exc).__name__}: {exc}".rstrip(": "))

            if attempt >= self._max_retries:
                raise error  # type: ignore[misc]
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt) * random.uniform(0.8, 1.2)
            log.warning(
                "status.tw request failed, retrying",
                extra=fields(path=_safe_path(path), error=str(error), attempt=attempt + 1, delay=f"{delay:.1f}"),
            )
            attempt += 1
            await asyncio.sleep(delay)

    def _observe_rate_headers(self, resp: aiohttp.ClientResponse) -> None:
        """Honour `x-ratelimit-remaining` / `x-ratelimit-reset` as sent by status.tw (observed, not documented)."""
        remaining = _header_float(resp, "x-ratelimit-remaining")
        reset = _header_float(resp, "x-ratelimit-reset")
        if remaining is not None and remaining <= 0 and reset is not None:
            self.budget.block_for(reset)
            log.info("status.tw request quota exhausted, pausing", extra=fields(seconds=reset))

    @staticmethod
    def _retry_after(resp: aiohttp.ClientResponse) -> float:
        for header in ("Retry-After", "x-ratelimit-reset"):
            value = _header_float(resp, header)
            if value is not None and value >= 0:
                return min(value, 300.0)
        return 60.0  # documented window length


def _header_float(resp: aiohttp.ClientResponse, name: str) -> float | None:
    raw = resp.headers.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _safe_path(path: str) -> str:
    """Path for logging (player names are not secret, but keep log lines short)."""
    return path if len(path) <= 80 else path[:77] + "..."
