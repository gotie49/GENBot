"""Consistent online backup of the SQLite database (safe while the bot is running).

Usage: ``python -m statustw_bot.backup [TARGET]``
Default target: ``<DATABASE_PATH dir>/backup-YYYYmmdd-HHMMSS.sqlite3``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime


def backup(source: str, target: str) -> None:
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def main(argv: list[str]) -> int:
    source = os.environ.get("DATABASE_PATH", "data/bot.sqlite3")
    if not os.path.exists(source):
        print(f"database not found: {source}", file=sys.stderr)
        return 1
    if len(argv) > 1:
        target = argv[1]
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(os.path.dirname(os.path.abspath(source)), f"backup-{stamp}.sqlite3")
    backup(source, target)
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
