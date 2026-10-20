"""Entry point: ``python -m statustw_bot``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import aiohttp
import discord

from . import __version__
from .bot import StatusBot
from .config import ConfigError, Settings, load_dotenv
from .logging_setup import fields, setup_logging

log = logging.getLogger("statustw_bot")


async def run(settings: Settings) -> int:
    bot = StatusBot(settings)
    loop = asyncio.get_running_loop()

    def request_shutdown(signame: str) -> None:
        log.info("signal received, stopping", extra=fields(signal=signame))
        loop.create_task(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C still raises KeyboardInterrupt

    try:
        async with bot:
            await bot.start(settings.discord_bot_token)
    except discord.LoginFailure:
        log.error("Discord rejected the bot token (DISCORD_BOT_TOKEN)")
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error("Discord refused the requested gateway intents")
        return 1
    except (aiohttp.ClientError, OSError) as exc:
        # Exit non-zero; Docker's restart policy retries with increasing delay.
        log.error("cannot connect to Discord", extra=fields(error=repr(exc)))
        return 1
    return 0


def main() -> int:
    load_dotenv()  # local runs only; the Docker image contains no .env (compose passes env_file)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, settings.secrets)
    log.info(
        "starting statustw-discord-bot",
        extra=fields(version=__version__, discord_py=discord.__version__, python=sys.version.split()[0]),
    )
    log.debug("effective settings", extra=fields(settings=repr(settings)))
    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
