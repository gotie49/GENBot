"""SQLite persistence (aiosqlite: queries run on a worker thread, not in the event loop)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
import aiosqlite

from .logging_setup import fields
from .presence import PresenceState, Status

log = logging.getLogger(__name__)

# Each entry migrates from version i to i+1. Append only; never edit released migrations.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE players (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        name                TEXT    NOT NULL UNIQUE,
        enabled             INTEGER NOT NULL DEFAULT 1,
        profile_url         TEXT,
        server_filter       TEXT,
        added_by            INTEGER,
        created_at          TEXT    NOT NULL,
        status              TEXT    NOT NULL DEFAULT 'unknown',
        status_changed_at   TEXT,
        last_checked_at     TEXT,
        last_online_at      TEXT,
        last_server_name    TEXT,
        last_server_address TEXT,
        offline_streak      INTEGER NOT NULL DEFAULT 0,
        consecutive_errors  INTEGER NOT NULL DEFAULT 0,
        session_announced   INTEGER NOT NULL DEFAULT 0,
        last_notified_at    TEXT
    );
    """,
)


class PlayerAlreadyExists(Exception):
    pass


@dataclass(frozen=True)
class PlayerRecord:
    id: int
    name: str
    enabled: bool
    profile_url: str | None
    server_filter: str | None
    added_by: int | None
    created_at: datetime
    state: PresenceState


def _ts(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_record(row: aiosqlite.Row) -> PlayerRecord:
    try:
        status = Status(row["status"])
    except ValueError:
        status = Status.UNKNOWN
    return PlayerRecord(
        id=row["id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        profile_url=row["profile_url"],
        server_filter=row["server_filter"],
        added_by=row["added_by"],
        created_at=_dt(row["created_at"]) or datetime.now(timezone.utc),
        state=PresenceState(
            status=status,
            status_changed_at=_dt(row["status_changed_at"]),
            last_checked_at=_dt(row["last_checked_at"]),
            last_online_at=_dt(row["last_online_at"]),
            last_server_name=row["last_server_name"],
            last_server_address=row["last_server_address"],
            offline_streak=row["offline_streak"],
            consecutive_errors=row["consecutive_errors"],
            session_announced=bool(row["session_announced"]),
            last_notified_at=_dt(row["last_notified_at"]),
        ),
    )


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.open() must be called first")
        return self._db

    async def open(self) -> None:
        if self._path != ":memory:":
            directory = os.path.dirname(os.path.abspath(self._path))
            os.makedirs(directory, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._migrate()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _migrate(self) -> None:
        async with self.db.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        version = int(row[0]) if row else 0
        if version > len(MIGRATIONS):
            raise RuntimeError(
                f"database schema version {version} is newer than this bot supports ({len(MIGRATIONS)})"
            )
        for target in range(version, len(MIGRATIONS)):
            await self.db.executescript(f"BEGIN; {MIGRATIONS[target]} PRAGMA user_version = {target + 1}; COMMIT;")
            log.info("database migrated", extra=fields(version=target + 1, path=self._path))

    # ------------------------------------------------------------------ queries

    async def add_player(
        self,
        name: str,
        *,
        added_by: int | None = None,
        profile_url: str | None = None,
        server_filter: str | None = None,
        now: datetime | None = None,
    ) -> PlayerRecord:
        now = now or datetime.now(timezone.utc)
        try:
            cur = await self.db.execute(
                "INSERT INTO players (name, profile_url, server_filter, added_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, profile_url, server_filter, added_by, _ts(now)),
            )
            await self.db.commit()
        except aiosqlite.IntegrityError as exc:
            raise PlayerAlreadyExists(name) from exc
        record = await self.get_player_by_id(int(cur.lastrowid or 0))
        assert record is not None
        return record

    async def remove_player(self, name: str) -> bool:
        cur = await self.db.execute("DELETE FROM players WHERE name = ?", (name,))
        await self.db.commit()
        return cur.rowcount == 1

    async def set_enabled(self, name: str, enabled: bool) -> PlayerRecord | None:
        """Enable/disable. Enabling resets the presence state so a stale state can't trigger a notification."""
        if enabled:
            sql = (
                "UPDATE players SET enabled = 1, status = 'unknown', offline_streak = 0, "
                "consecutive_errors = 0, session_announced = 0 WHERE name = ? AND enabled = 0"
            )
        else:
            sql = "UPDATE players SET enabled = 0 WHERE name = ? AND enabled = 1"
        await self.db.execute(sql, (name,))
        await self.db.commit()
        return await self.get_player(name)

    async def get_player(self, name: str) -> PlayerRecord | None:
        async with self.db.execute("SELECT * FROM players WHERE name = ?", (name,)) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def get_player_by_id(self, player_id: int) -> PlayerRecord | None:
        async with self.db.execute("SELECT * FROM players WHERE id = ?", (player_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def list_players(self, *, enabled_only: bool = False) -> list[PlayerRecord]:
        sql = "SELECT * FROM players" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY name COLLATE NOCASE"
        async with self.db.execute(sql) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def search_names(self, prefix: str, limit: int = 25) -> list[str]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self.db.execute(
            "SELECT name FROM players WHERE name LIKE ? ESCAPE '\\' ORDER BY name COLLATE NOCASE LIMIT ?",
            (f"%{escaped}%", limit),
        ) as cur:
            rows = await cur.fetchall()
        return [r["name"] for r in rows]

    async def save_state(self, player_id: int, state: PresenceState) -> bool:
        """Persist a new presence state.

        Only applies while the player still exists and is enabled, so a concurrent
        ``/player remove`` or ``/player disable`` wins over an in-flight poll. Returns
        False if nothing was written (the caller must then not notify).
        """
        cur = await self.db.execute(
            """
            UPDATE players SET
                status = ?, status_changed_at = ?, last_checked_at = ?, last_online_at = ?,
                last_server_name = ?, last_server_address = ?, offline_streak = ?,
                consecutive_errors = ?, session_announced = ?, last_notified_at = ?
            WHERE id = ? AND enabled = 1
            """,
            (
                state.status.value,
                _ts(state.status_changed_at),
                _ts(state.last_checked_at),
                _ts(state.last_online_at),
                state.last_server_name,
                state.last_server_address,
                state.offline_streak,
                state.consecutive_errors,
                int(state.session_announced),
                _ts(state.last_notified_at),
                player_id,
            ),
        )
        await self.db.commit()
        return cur.rowcount == 1

