"""Structured (key=value) logging to stdout with secret redaction."""

from __future__ import annotations

import logging
import sys
from typing import Any, Iterable

REDACTED = "***REDACTED***"


class RedactingFilter(logging.Filter):
    """Replaces any configured secret value in the final log message."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s and len(s) >= 6)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


class KeyValueFormatter(logging.Formatter):
    """`ts=... level=... logger=... msg="..." key=value ...` lines.

    Extra fields are passed as ``logger.info("msg", extra={"fields": {...}})``.
    """

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            f"ts={self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z')}",
            f"level={record.levelname}",
            f"logger={record.name}",
            f"msg={_quote(record.getMessage())}",
        ]
        fields: dict[str, Any] = getattr(record, "fields", None) or {}
        for key, value in fields.items():
            parts.append(f"{key}={_quote(value)}")
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _quote(value: Any) -> str:
    text = str(value)
    if text == "" or any(c in text for c in ' "=\n\t'):
        text = '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return text


def setup_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(KeyValueFormatter())
    handler.addFilter(RedactingFilter(secrets))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # discord.py is very chatty on DEBUG (raw gateway payloads); cap it at INFO.
    logging.getLogger("discord").setLevel(max(logging.INFO, root.level))
    # This bot never uses voice; hide the "PyNaCl/davey is not installed" warnings.
    logging.getLogger("discord.client").addFilter(lambda r: "voice will NOT be supported" not in r.getMessage())
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def fields(**kwargs: Any) -> dict[str, dict[str, Any]]:
    """Helper: ``log.info("...", extra=fields(player="x"))``."""
    return {"fields": kwargs}
