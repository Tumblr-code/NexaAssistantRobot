from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS warnings (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
    last_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS trusted_users (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS allowed_domains (
    chat_id INTEGER NOT NULL,
    domain TEXT NOT NULL,
    added_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, domain)
);

CREATE TABLE IF NOT EXISTS blocked_words (
    chat_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    added_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, word)
);

CREATE TABLE IF NOT EXISTS moderation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    actor_id INTEGER,
    target_id INTEGER,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_moderation_log_chat_created
ON moderation_log (chat_id, created_at);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class ChatStats:
    deleted: int
    warned: int
    muted: int
    banned: int
    total_actions: int


@dataclass(frozen=True, slots=True)
class WarningStats:
    users: int
    total: int


@dataclass(frozen=True, slots=True)
class RecentAction:
    action: str
    actor_id: int | None
    target_id: int | None
    message_id: int | None
    created_at: str


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("数据库尚未初始化")
        return self._db

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def add_warning(self, chat_id: int, user_id: int) -> int:
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO warnings (chat_id, user_id, count, last_reason, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    count = warnings.count + 1,
                    last_reason = excluded.last_reason,
                    updated_at = excluded.updated_at
                """,
                (chat_id, user_id, "", utc_now()),
            )
            cursor = await self.db.execute(
                "SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            row = await cursor.fetchone()
            await self.db.commit()
        return int(row["count"])

    async def warning_count(self, chat_id: int, user_id: int) -> int:
        cursor = await self.db.execute(
            "SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = await cursor.fetchone()
        return int(row["count"]) if row else 0

    async def clear_warnings(self, chat_id: int, user_id: int) -> None:
        async with self._write_lock:
            await self.db.execute(
                "DELETE FROM warnings WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            await self.db.commit()

    async def is_trusted(self, chat_id: int, user_id: int) -> bool:
        cursor = await self.db.execute(
            "SELECT 1 FROM trusted_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        return await cursor.fetchone() is not None

    async def add_trusted_user(self, chat_id: int, user_id: int, actor_id: int) -> None:
        await self._upsert_pair("trusted_users", chat_id, user_id, actor_id)

    async def remove_trusted_user(self, chat_id: int, user_id: int) -> None:
        async with self._write_lock:
            await self.db.execute(
                "DELETE FROM trusted_users WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            await self.db.commit()

    async def _upsert_pair(self, table: str, chat_id: int, user_id: int, actor_id: int) -> None:
        if table != "trusted_users":
            raise ValueError("unsupported table")
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO trusted_users (chat_id, user_id, added_by, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    added_by = excluded.added_by,
                    created_at = excluded.created_at
                """,
                (chat_id, user_id, actor_id, utc_now()),
            )
            await self.db.commit()

    async def allowed_domains(self, chat_id: int) -> set[str]:
        cursor = await self.db.execute(
            "SELECT domain FROM allowed_domains WHERE chat_id = ? ORDER BY domain",
            (chat_id,),
        )
        return {str(row["domain"]) for row in await cursor.fetchall()}

    async def add_allowed_domain(self, chat_id: int, domain: str, actor_id: int) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO allowed_domains (chat_id, domain, added_by, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, domain) DO UPDATE SET
                    added_by = excluded.added_by,
                    created_at = excluded.created_at
                """,
                (chat_id, domain, actor_id, utc_now()),
            )
            await self.db.commit()

    async def remove_allowed_domain(self, chat_id: int, domain: str) -> None:
        async with self._write_lock:
            await self.db.execute(
                "DELETE FROM allowed_domains WHERE chat_id = ? AND domain = ?",
                (chat_id, domain),
            )
            await self.db.commit()

    async def blocked_words(self, chat_id: int) -> set[str]:
        cursor = await self.db.execute(
            "SELECT word FROM blocked_words WHERE chat_id = ? ORDER BY word",
            (chat_id,),
        )
        return {str(row["word"]) for row in await cursor.fetchall()}

    async def add_blocked_word(self, chat_id: int, word: str, actor_id: int) -> None:
        await self.add_blocked_words(chat_id, (word,), actor_id)

    async def add_blocked_words(
        self, chat_id: int, words: tuple[str, ...], actor_id: int
    ) -> None:
        timestamp = utc_now()
        async with self._write_lock:
            await self.db.executemany(
                """
                INSERT INTO blocked_words (chat_id, word, added_by, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, word) DO UPDATE SET
                    added_by = excluded.added_by,
                    created_at = excluded.created_at
                """,
                ((chat_id, word, actor_id, timestamp) for word in words),
            )
            await self.db.commit()

    async def remove_blocked_word(self, chat_id: int, word: str) -> None:
        await self.remove_blocked_words(chat_id, (word,))

    async def remove_blocked_words(self, chat_id: int, words: tuple[str, ...]) -> None:
        async with self._write_lock:
            await self.db.executemany(
                "DELETE FROM blocked_words WHERE chat_id = ? AND word = ?",
                ((chat_id, word) for word in words),
            )
            await self.db.commit()

    async def record_action(
        self,
        *,
        chat_id: int,
        action: str,
        message_id: int | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
    ) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO moderation_log
                    (chat_id, message_id, actor_id, target_id, action, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, message_id, actor_id, target_id, action, "", utc_now()),
            )
            await self.db.commit()

    async def chat_stats(self, chat_id: int, *, since: str | None = None) -> ChatStats:
        query = """
            SELECT
                SUM(CASE WHEN action = 'delete' THEN 1 ELSE 0 END) AS deleted,
                SUM(CASE WHEN action = 'warn' THEN 1 ELSE 0 END) AS warned,
                SUM(CASE WHEN action IN ('mute', 'auto_mute') THEN 1 ELSE 0 END) AS muted,
                SUM(CASE WHEN action = 'ban' THEN 1 ELSE 0 END) AS banned,
                COUNT(*) AS total_actions
            FROM moderation_log
            WHERE chat_id = ?
        """
        parameters: tuple[int | str, ...] = (chat_id,)
        if since is not None:
            query += " AND created_at >= ?"
            parameters = (chat_id, since)
        cursor = await self.db.execute(
            query,
            parameters,
        )
        row = await cursor.fetchone()
        return ChatStats(
            deleted=int(row["deleted"] or 0),
            warned=int(row["warned"] or 0),
            muted=int(row["muted"] or 0),
            banned=int(row["banned"] or 0),
            total_actions=int(row["total_actions"] or 0),
        )

    async def warning_stats(self, chat_id: int) -> WarningStats:
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS users, COALESCE(SUM(count), 0) AS total
            FROM warnings
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        row = await cursor.fetchone()
        return WarningStats(users=int(row["users"]), total=int(row["total"]))

    async def recent_actions(self, chat_id: int, *, limit: int) -> tuple[RecentAction, ...]:
        cursor = await self.db.execute(
            """
            SELECT action, actor_id, target_id, message_id, created_at
            FROM moderation_log
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        return tuple(
            RecentAction(
                action=str(row["action"]),
                actor_id=int(row["actor_id"]) if row["actor_id"] is not None else None,
                target_id=int(row["target_id"]) if row["target_id"] is not None else None,
                message_id=int(row["message_id"]) if row["message_id"] is not None else None,
                created_at=str(row["created_at"]),
            )
            for row in await cursor.fetchall()
        )
