from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from nexa_guardian.ai_contract import LOCAL_REVIEW_MODELS

SCHEMA_VERSION = 9
UTC = timezone.utc
# A gateway call is any decision whose model is not one of the local, no-request paths.
AI_GATEWAY_MODEL_PARAMETERS = tuple(sorted(LOCAL_REVIEW_MODELS))
AI_GATEWAY_MODEL_PLACEHOLDERS = ", ".join("?" for _ in AI_GATEWAY_MODEL_PARAMETERS)
AI_GATEWAY_MODEL_PREDICATE = f"model NOT IN ({AI_GATEWAY_MODEL_PLACEHOLDERS})"

AI_LEARNING_EVIDENCE_TYPES = (
    "text",
    "image",
    "link",
    "mention",
    "forward",
    "contact",
    "obfuscation",
    "promotion",
    "flood",
    "duplicate",
)
AI_LEARNING_EVIDENCE_SET = frozenset(AI_LEARNING_EVIDENCE_TYPES)

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

CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    target_id INTEGER,
    is_ad INTEGER,
    confidence REAL,
    category TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    enforced INTEGER NOT NULL DEFAULT 0,
    error_type TEXT NOT NULL DEFAULT '',
    reasoning_effort TEXT NOT NULL DEFAULT '',
    service_tier TEXT NOT NULL DEFAULT '',
    review_path TEXT NOT NULL DEFAULT 'legacy' CHECK (review_path <> ''),
    media_latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (media_latency_ms >= 0),
    queue_latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (queue_latency_ms >= 0),
    request_latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (request_latency_ms >= 0),
    action_latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (action_latency_ms >= 0),
    total_latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (total_latency_ms >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_decisions_chat_created
ON ai_decisions (chat_id, created_at);

CREATE TABLE IF NOT EXISTS ai_totals (
    chat_id INTEGER PRIMARY KEY,
    decisions INTEGER NOT NULL DEFAULT 0 CHECK (decisions >= 0),
    calls INTEGER NOT NULL DEFAULT 0 CHECK (calls >= 0),
    ads INTEGER NOT NULL DEFAULT 0 CHECK (ads >= 0),
    enforced INTEGER NOT NULL DEFAULT 0 CHECK (enforced >= 0),
    failed INTEGER NOT NULL DEFAULT 0 CHECK (failed >= 0),
    cached INTEGER NOT NULL DEFAULT 0 CHECK (cached >= 0),
    latency_total_ms INTEGER NOT NULL DEFAULT 0 CHECK (latency_total_ms >= 0),
    latency_samples INTEGER NOT NULL DEFAULT 0 CHECK (latency_samples >= 0)
);

CREATE TABLE IF NOT EXISTS ai_signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    normalized_signature TEXT NOT NULL,
    display_signature TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'rejected', 'disabled')),
    max_confidence REAL NOT NULL DEFAULT 0,
    hit_count INTEGER NOT NULL DEFAULT 1 CHECK (hit_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (chat_id, normalized_signature)
);

CREATE INDEX IF NOT EXISTS idx_ai_signatures_chat_status
ON ai_signatures (chat_id, status, id);

CREATE TABLE IF NOT EXISTS ai_learning_memory (
    chat_id INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (category <> ''),
    evidence_profile TEXT NOT NULL CHECK (evidence_profile <> ''),
    decision_count INTEGER NOT NULL DEFAULT 1 CHECK (decision_count >= 1),
    max_confidence REAL NOT NULL CHECK (max_confidence >= 0 AND max_confidence <= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (chat_id, category, evidence_profile)
);

CREATE TABLE IF NOT EXISTS ephemeral_bot_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_ephemeral_delete_at
ON ephemeral_bot_messages (delete_at);

CREATE TABLE IF NOT EXISTS runtime_settings (
    chat_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS log_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    actor_id INTEGER,
    target_id INTEGER,
    action TEXT NOT NULL,
    due_at TEXT NOT NULL,
    claimed_until TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_log_outbox_due
ON log_outbox (due_at, claimed_until, id);

CREATE TABLE IF NOT EXISTS member_activity (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_message_at TEXT,
    last_membership_at TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_member_activity_chat_last_message
ON member_activity (chat_id, last_message_at);

CREATE INDEX IF NOT EXISTS idx_member_activity_chat_first_observed
ON member_activity (chat_id, first_observed_at);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def _metadata_token(name: str, value: str, *, allow_empty: bool) -> str:
    normalized = value.strip()
    if not normalized and allow_empty:
        return ""
    if (
        not normalized
        or len(normalized) > 64
        or any(
            not (character.isascii() and (character.isalnum() or character in "_.-"))
            for character in normalized
        )
    ):
        raise ValueError(f"{name} 必须是安全的短标识")
    return normalized


@dataclass(frozen=True, slots=True)
class ChatStats:
    deleted: int
    warned: int
    banned: int
    removed: int
    cleanup_failures: int
    history_purges: int


@dataclass(frozen=True, slots=True)
class WarningStats:
    users: int
    total: int


@dataclass(frozen=True, slots=True)
class AIStats:
    total: int
    calls: int
    ads: int
    enforced: int
    failed: int
    cached: int
    average_latency_ms: int
    reasoning_effort_counts: tuple[tuple[str, int], ...] = ()
    review_path_counts: tuple[tuple[str, int], ...] = ()
    gateway_latency_p50_ms: int = 0
    gateway_latency_p95_ms: int = 0
    total_latency_p50_ms: int = 0
    total_latency_p95_ms: int = 0


@dataclass(frozen=True, slots=True)
class AISignature:
    id: int
    normalized_signature: str
    display_signature: str
    category: str
    status: str
    max_confidence: float
    hit_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AILearningMemory:
    chat_id: int
    category: str
    evidence_profile: str
    decision_count: int
    max_confidence: float
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class EphemeralRecord:
    chat_id: int
    message_id: int
    delete_at: str


@dataclass(frozen=True, slots=True)
class LogOutboxRecord:
    id: int
    chat_id: int
    message_id: int | None
    actor_id: int | None
    target_id: int | None
    action: str
    due_at: str
    attempt_count: int
    created_at: str


@dataclass(frozen=True, slots=True)
class QueueHealth:
    ephemeral_total: int
    ephemeral_due: int
    log_outbox_total: int
    log_outbox_due: int
    log_outbox_retrying: int


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

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize writes and always close the transaction on cancellation or error."""

        async with self._write_lock:
            try:
                yield self.db
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        try:
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._db.execute("PRAGMA busy_timeout=5000")
            cursor = await self._db.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            current_version = int(row[0]) if row is not None else 0
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(f"数据库版本 {current_version} 高于程序支持的 {SCHEMA_VERSION}")
            await self._db.executescript(SCHEMA)
            await self._ensure_ephemeral_schema()
            await self._ensure_ai_decisions_schema(current_version)
            if current_version < 4:
                await self._rebuild_ai_totals()
            if current_version < 7:
                await self._migrate_v7()
            if current_version < 9:
                await self._migrate_v9()
            await self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise

    async def _ensure_ai_decisions_schema(self, current_version: int) -> None:
        cursor = await self.db.execute("PRAGMA table_info(ai_decisions)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        additions = {
            "reasoning_effort": "TEXT NOT NULL DEFAULT ''",
            "service_tier": "TEXT NOT NULL DEFAULT ''",
            "review_path": "TEXT NOT NULL DEFAULT 'legacy' CHECK (review_path <> '')",
            "media_latency_ms": ("INTEGER NOT NULL DEFAULT 0 CHECK (media_latency_ms >= 0)"),
            "queue_latency_ms": ("INTEGER NOT NULL DEFAULT 0 CHECK (queue_latency_ms >= 0)"),
            "request_latency_ms": ("INTEGER NOT NULL DEFAULT 0 CHECK (request_latency_ms >= 0)"),
            "action_latency_ms": ("INTEGER NOT NULL DEFAULT 0 CHECK (action_latency_ms >= 0)"),
            "total_latency_ms": ("INTEGER NOT NULL DEFAULT 0 CHECK (total_latency_ms >= 0)"),
            "attempt_count": "INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)",
        }
        for name, definition in additions.items():
            if name not in columns:
                await self.db.execute(f"ALTER TABLE ai_decisions ADD COLUMN {name} {definition}")  # noqa: S608 - names and definitions come from a fixed internal mapping
        if current_version < 6:
            await self.db.execute(
                """
                UPDATE ai_decisions
                SET review_path = 'legacy',
                    request_latency_ms = MAX(latency_ms, 0),
                    total_latency_ms = MAX(latency_ms, 0)
                """
            )

    async def _ensure_ephemeral_schema(self) -> None:
        cursor = await self.db.execute("PRAGMA table_info(ephemeral_bot_messages)")
        columns = tuple(str(row["name"]) for row in await cursor.fetchall())
        expected = ("chat_id", "message_id", "delete_at")
        if columns == expected:
            return
        if not set(expected).issubset(columns):
            raise RuntimeError("临时消息队列表结构不兼容")

        await self.db.executescript(
            """
            BEGIN IMMEDIATE;
            DROP INDEX IF EXISTS idx_ephemeral_delete_at;
            ALTER TABLE ephemeral_bot_messages RENAME TO ephemeral_bot_messages_legacy;
            CREATE TABLE ephemeral_bot_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            INSERT OR IGNORE INTO ephemeral_bot_messages (chat_id, message_id, delete_at)
            SELECT chat_id, message_id, delete_at FROM ephemeral_bot_messages_legacy;
            DROP TABLE ephemeral_bot_messages_legacy;
            CREATE INDEX idx_ephemeral_delete_at
            ON ephemeral_bot_messages (delete_at);
            COMMIT;
            """
        )

    async def _rebuild_ai_totals(self) -> None:
        await self.db.execute("DELETE FROM ai_totals")
        await self.db.execute(
            f"""
            INSERT INTO ai_totals (
                chat_id, decisions, calls, ads, enforced, failed, cached,
                latency_total_ms, latency_samples
            )
            SELECT
                chat_id,
                COUNT(*),
                SUM(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE} AND cached = 0 THEN 1 ELSE 0
                END),
                SUM(CASE WHEN is_ad = 1 THEN 1 ELSE 0 END),
                SUM(enforced),
                SUM(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE} AND error_type <> '' THEN 1 ELSE 0
                END),
                SUM(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE} AND cached = 1 THEN 1 ELSE 0
                END),
                SUM(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE}
                         AND cached = 0 AND error_type = ''
                    THEN latency_ms ELSE 0
                END),
                SUM(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE}
                         AND cached = 0 AND error_type = ''
                    THEN 1 ELSE 0
                END)
            FROM ai_decisions
            GROUP BY chat_id
            """,  # noqa: S608 - placeholders come from a fixed internal model allowlist
            AI_GATEWAY_MODEL_PARAMETERS * 5,
        )

    async def _migrate_v7(self) -> None:
        """Remove retired operational history while preserving learned AI state."""

        await self.db.execute("PRAGMA secure_delete=ON")
        for table in (
            "warnings",
            "moderation_log",
            "ai_decisions",
            "ai_totals",
            "log_outbox",
            "trusted_users",
            "allowed_domains",
            "blocked_words",
            "member_activity",
            "runtime_settings",
        ):
            await self.db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed internal names
        await self.db.execute(
            """
            DELETE FROM sqlite_sequence
            WHERE name IN ('moderation_log', 'ai_decisions', 'log_outbox')
            """
        )

    async def _migrate_v9(self) -> None:
        """Drop the tables and markers of the removed raffle feature."""

        await self.db.executescript(
            """
            DROP INDEX IF EXISTS idx_raffles_due;
            DROP INDEX IF EXISTS idx_raffles_keyword;
            DROP INDEX IF EXISTS idx_raffles_one_running_per_chat;
            DROP TABLE IF EXISTS raffle_winners;
            DROP TABLE IF EXISTS raffle_entries;
            DROP TABLE IF EXISTS raffles;
            DELETE FROM runtime_settings WHERE key LIKE 'raffle\\_%' ESCAPE '\\';
            """
        )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def add_warning_and_record_action(
        self,
        chat_id: int,
        user_id: int,
        actor_id: int | None = None,
        *,
        enqueue_log: bool = False,
        log_due_at: str | None = None,
    ) -> int:
        """Atomically increment a warning and append its moderation action."""

        timestamp = utc_now()
        async with self._write_transaction() as db:
            await db.execute(
                """
                INSERT INTO warnings (chat_id, user_id, count, last_reason, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    count = warnings.count + 1,
                    last_reason = excluded.last_reason,
                    updated_at = excluded.updated_at
                """,
                (chat_id, user_id, "", timestamp),
            )
            cursor = await db.execute(
                "SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            row = await cursor.fetchone()
            await self._insert_action(
                db,
                chat_id=chat_id,
                action="warn",
                actor_id=actor_id,
                target_id=user_id,
                timestamp=timestamp,
            )
            if enqueue_log:
                await self._insert_log_outbox(
                    db,
                    chat_id=chat_id,
                    action="warn",
                    actor_id=actor_id,
                    target_id=user_id,
                    due_at=log_due_at or timestamp,
                    timestamp=timestamp,
                )
        if row is None:
            raise RuntimeError("警告写入后读取失败")
        return int(row["count"])

    async def clear_warnings(self, chat_id: int, user_id: int) -> None:
        async with self._write_transaction() as db:
            await db.execute(
                "DELETE FROM warnings WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )

    async def is_trusted(self, chat_id: int, user_id: int) -> bool:
        cursor = await self.db.execute(
            "SELECT 1 FROM trusted_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        return await cursor.fetchone() is not None

    async def trusted_user_ids(self, chat_id: int) -> frozenset[int]:
        cursor = await self.db.execute(
            "SELECT user_id FROM trusted_users WHERE chat_id = ? ORDER BY user_id",
            (chat_id,),
        )
        return frozenset(int(row["user_id"]) for row in await cursor.fetchall())

    async def allowed_domains(self, chat_id: int) -> set[str]:
        cursor = await self.db.execute(
            "SELECT domain FROM allowed_domains WHERE chat_id = ? ORDER BY domain",
            (chat_id,),
        )
        return {str(row["domain"]) for row in await cursor.fetchall()}

    async def blocked_words(self, chat_id: int) -> set[str]:
        cursor = await self.db.execute(
            "SELECT word FROM blocked_words WHERE chat_id = ? ORDER BY word",
            (chat_id,),
        )
        return {str(row["word"]) for row in await cursor.fetchall()}

    async def record_action(
        self,
        *,
        chat_id: int,
        action: str,
        message_id: int | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
        enqueue_log: bool = False,
        log_due_at: str | None = None,
    ) -> None:
        timestamp = utc_now()
        async with self._write_transaction() as db:
            await self._insert_action(
                db,
                chat_id=chat_id,
                action=action,
                message_id=message_id,
                actor_id=actor_id,
                target_id=target_id,
                timestamp=timestamp,
            )
            if enqueue_log:
                await self._insert_log_outbox(
                    db,
                    chat_id=chat_id,
                    action=action,
                    message_id=message_id,
                    actor_id=actor_id,
                    target_id=target_id,
                    due_at=log_due_at or timestamp,
                    timestamp=timestamp,
                )

    @staticmethod
    async def _insert_action(
        db: aiosqlite.Connection,
        *,
        chat_id: int,
        action: str,
        timestamp: str,
        message_id: int | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
    ) -> None:
        await db.execute(
            """
            INSERT INTO moderation_log
                (chat_id, message_id, actor_id, target_id, action, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, message_id, actor_id, target_id, action, "", timestamp),
        )

    @staticmethod
    async def _insert_log_outbox(
        db: aiosqlite.Connection,
        *,
        chat_id: int,
        action: str,
        due_at: str,
        timestamp: str,
        message_id: int | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
    ) -> None:
        await db.execute(
            """
            INSERT INTO log_outbox (
                chat_id, message_id, actor_id, target_id, action,
                due_at, claimed_until, attempt_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?)
            """,
            (chat_id, message_id, actor_id, target_id, action, due_at, timestamp),
        )

    async def chat_stats(self, chat_id: int, *, since: str | None = None) -> ChatStats:
        query = """
            SELECT
                SUM(CASE WHEN action = 'delete' THEN 1 ELSE 0 END) AS deleted,
                SUM(CASE WHEN action = 'warn' THEN 1 ELSE 0 END) AS warned,
                SUM(CASE WHEN action IN ('ban', 'auto_ban') THEN 1 ELSE 0 END) AS banned,
                SUM(CASE WHEN action IN (
                    'member_cleanup', 'cleanup_inactive', 'kick'
                ) THEN 1 ELSE 0 END) AS removed,
                SUM(CASE WHEN action IN (
                    'member_cleanup_unban_failed',
                    'cleanup_inactive_unban_failed',
                    'kick_unban_failed'
                ) THEN 1 ELSE 0 END) AS cleanup_failures,
                SUM(CASE WHEN action = 'history_purge' THEN 1 ELSE 0 END) AS history_purges
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
        if row is None:
            return ChatStats(0, 0, 0, 0, 0, 0)
        return ChatStats(
            deleted=int(row["deleted"] or 0),
            warned=int(row["warned"] or 0),
            banned=int(row["banned"] or 0),
            removed=int(row["removed"] or 0),
            cleanup_failures=int(row["cleanup_failures"] or 0),
            history_purges=int(row["history_purges"] or 0),
        )

    async def warning_stats(self, chat_id: int) -> WarningStats:
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) AS users, COALESCE(SUM(count), 0) AS total
            FROM warnings
            WHERE chat_id = ? AND count > 0
            """,
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return WarningStats(0, 0)
        return WarningStats(users=int(row["users"]), total=int(row["total"]))

    async def record_ai_decision(
        self,
        *,
        chat_id: int,
        message_id: int | None,
        target_id: int | None,
        is_ad: bool | None,
        confidence: float | None,
        category: str,
        model: str,
        latency_ms: int,
        cached: bool,
        enforced: bool,
        error_type: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        review_path: str = "legacy",
        media_latency_ms: int = 0,
        queue_latency_ms: int = 0,
        request_latency_ms: int | None = None,
        action_latency_ms: int = 0,
        total_latency_ms: int | None = None,
        attempt_count: int = 0,
    ) -> None:
        normalized_reasoning_effort = _metadata_token(
            "reasoning_effort", reasoning_effort, allow_empty=True
        )
        normalized_service_tier = _metadata_token("service_tier", service_tier, allow_empty=True)
        normalized_review_path = _metadata_token("review_path", review_path, allow_empty=False)
        is_gateway = model not in LOCAL_REVIEW_MODELS
        gateway_call = int(is_gateway and not cached)
        gateway_cache = int(is_gateway and cached)
        gateway_failure = int(is_gateway and bool(error_type))
        latency_sample = int(gateway_call == 1 and not error_type)
        safe_latency_ms = max(0, latency_ms)
        safe_media_latency_ms = _nonnegative_int("media_latency_ms", media_latency_ms)
        safe_queue_latency_ms = _nonnegative_int("queue_latency_ms", queue_latency_ms)
        safe_request_latency_ms = _nonnegative_int(
            "request_latency_ms",
            safe_latency_ms if request_latency_ms is None else request_latency_ms,
        )
        safe_action_latency_ms = _nonnegative_int("action_latency_ms", action_latency_ms)
        safe_total_latency_ms = _nonnegative_int(
            "total_latency_ms",
            safe_latency_ms if total_latency_ms is None else total_latency_ms,
        )
        safe_attempt_count = _nonnegative_int("attempt_count", attempt_count)
        async with self._write_transaction() as db:
            await db.execute(
                """
                INSERT INTO ai_decisions (
                    chat_id, message_id, target_id, is_ad, confidence, category,
                    model, latency_ms, cached, enforced, error_type,
                    reasoning_effort, service_tier, review_path,
                    media_latency_ms, queue_latency_ms, request_latency_ms,
                    action_latency_ms, total_latency_ms, attempt_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    message_id,
                    target_id,
                    None if is_ad is None else int(is_ad),
                    confidence,
                    category,
                    model,
                    safe_latency_ms,
                    int(cached),
                    int(enforced),
                    error_type,
                    normalized_reasoning_effort,
                    normalized_service_tier,
                    normalized_review_path,
                    safe_media_latency_ms,
                    safe_queue_latency_ms,
                    safe_request_latency_ms,
                    safe_action_latency_ms,
                    safe_total_latency_ms,
                    safe_attempt_count,
                    utc_now(),
                ),
            )
            await db.execute(
                """
                INSERT INTO ai_totals (
                    chat_id, decisions, calls, ads, enforced, failed, cached,
                    latency_total_ms, latency_samples
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    decisions = ai_totals.decisions + 1,
                    calls = ai_totals.calls + excluded.calls,
                    ads = ai_totals.ads + excluded.ads,
                    enforced = ai_totals.enforced + excluded.enforced,
                    failed = ai_totals.failed + excluded.failed,
                    cached = ai_totals.cached + excluded.cached,
                    latency_total_ms = ai_totals.latency_total_ms + excluded.latency_total_ms,
                    latency_samples = ai_totals.latency_samples + excluded.latency_samples
                """,
                (
                    chat_id,
                    gateway_call,
                    int(is_ad is True),
                    int(enforced),
                    gateway_failure,
                    gateway_cache,
                    safe_latency_ms if latency_sample else 0,
                    latency_sample,
                ),
            )

    async def ai_stats(self, chat_id: int, *, since: str | None = None) -> AIStats:
        if since is None:
            cursor = await self.db.execute(
                """
                SELECT decisions, calls, ads, enforced, failed, cached,
                       latency_total_ms, latency_samples
                FROM ai_totals
                WHERE chat_id = ?
                """,
                (chat_id,),
            )
            total_row = await cursor.fetchone()
            if total_row is None:
                return AIStats(0, 0, 0, 0, 0, 0, 0)
            latency_samples = int(total_row["latency_samples"] or 0)
            average_latency_ms = (
                int(total_row["latency_total_ms"] or 0) // latency_samples if latency_samples else 0
            )
            return AIStats(
                total=int(total_row["decisions"] or 0),
                calls=int(total_row["calls"] or 0),
                ads=int(total_row["ads"] or 0),
                enforced=int(total_row["enforced"] or 0),
                failed=int(total_row["failed"] or 0),
                cached=int(total_row["cached"] or 0),
                average_latency_ms=average_latency_ms,
            )

        query = f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE} AND cached = 0 THEN 1 ELSE 0
                END) AS calls,
                SUM(CASE WHEN is_ad = 1 THEN 1 ELSE 0 END) AS ads,
                SUM(enforced) AS enforced,
                SUM(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE} AND error_type <> '' THEN 1 ELSE 0
                END) AS failed,
                SUM(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE} AND cached = 1 THEN 1 ELSE 0
                END) AS cached,
                AVG(CASE
                    WHEN {AI_GATEWAY_MODEL_PREDICATE}
                         AND cached = 0 AND error_type = ''
                    THEN latency_ms
                END) AS average_latency_ms
            FROM ai_decisions
            WHERE chat_id = ?
        """  # noqa: S608 - placeholders come from a fixed internal model allowlist
        query += " AND created_at >= ?"
        cursor = await self.db.execute(
            query,
            (*(AI_GATEWAY_MODEL_PARAMETERS * 4), chat_id, since),
        )
        row = await cursor.fetchone()
        if row is None:
            return AIStats(0, 0, 0, 0, 0, 0, 0)
        (
            reasoning_effort_counts,
            review_path_counts,
            gateway_percentiles,
            total_percentiles,
        ) = await self._ai_diagnostics(chat_id, since=since)
        return AIStats(
            total=int(row["total"] or 0),
            calls=int(row["calls"] or 0),
            ads=int(row["ads"] or 0),
            enforced=int(row["enforced"] or 0),
            failed=int(row["failed"] or 0),
            cached=int(row["cached"] or 0),
            average_latency_ms=int(row["average_latency_ms"] or 0),
            reasoning_effort_counts=reasoning_effort_counts,
            review_path_counts=review_path_counts,
            gateway_latency_p50_ms=gateway_percentiles[0],
            gateway_latency_p95_ms=gateway_percentiles[1],
            total_latency_p50_ms=total_percentiles[0],
            total_latency_p95_ms=total_percentiles[1],
        )

    async def _ai_diagnostics(
        self,
        chat_id: int,
        *,
        since: str,
    ) -> tuple[
        tuple[tuple[str, int], ...],
        tuple[tuple[str, int], ...],
        tuple[int, int],
        tuple[int, int],
    ]:
        effort_cursor = await self.db.execute(
            """
            SELECT CASE
                       WHEN reasoning_effort = '' THEN 'legacy'
                       ELSE reasoning_effort
                   END AS value,
                   COUNT(*) AS count
            FROM ai_decisions
            WHERE chat_id = ? AND created_at >= ?
            GROUP BY value
            ORDER BY value
            """,
            (chat_id, since),
        )
        path_cursor = await self.db.execute(
            """
            SELECT review_path AS value, COUNT(*) AS count
            FROM ai_decisions
            WHERE chat_id = ? AND created_at >= ?
            GROUP BY review_path
            ORDER BY review_path
            """,
            (chat_id, since),
        )
        effort_counts = tuple(
            (str(row["value"]), int(row["count"])) for row in await effort_cursor.fetchall()
        )
        path_counts = tuple(
            (str(row["value"]), int(row["count"])) for row in await path_cursor.fetchall()
        )
        gateway_percentiles = await self._latency_percentiles(
            chat_id,
            since=since,
            column="request_latency_ms",
            gateway_only=True,
        )
        total_percentiles = await self._latency_percentiles(
            chat_id,
            since=since,
            column="total_latency_ms",
            gateway_only=False,
        )
        return effort_counts, path_counts, gateway_percentiles, total_percentiles

    async def _latency_percentiles(
        self,
        chat_id: int,
        *,
        since: str,
        column: str,
        gateway_only: bool,
    ) -> tuple[int, int]:
        if column not in {"request_latency_ms", "total_latency_ms"}:
            raise ValueError("unsupported latency column")
        parameters: list[int | str] = [chat_id, since]
        gateway_filter = ""
        if gateway_only:
            gateway_filter = f" AND {AI_GATEWAY_MODEL_PREDICATE} AND cached = 0"
            parameters.extend(AI_GATEWAY_MODEL_PARAMETERS)
        cursor = await self.db.execute(
            f"""
            WITH ranked AS (
                SELECT {column} AS value,
                       ROW_NUMBER() OVER (ORDER BY {column}) AS row_number,
                       COUNT(*) OVER () AS sample_count
                FROM ai_decisions
                WHERE chat_id = ? AND created_at >= ?{gateway_filter}
            )
            SELECT
                COALESCE(MAX(CASE
                    WHEN row_number = CAST((sample_count * 50 + 99) / 100 AS INTEGER)
                    THEN value
                END), 0) AS p50,
                COALESCE(MAX(CASE
                    WHEN row_number = CAST((sample_count * 95 + 99) / 100 AS INTEGER)
                    THEN value
                END), 0) AS p95
            FROM ranked
            """,  # noqa: S608 - column and placeholders come from fixed internal allowlists
            tuple(parameters),
        )
        row = await cursor.fetchone()
        if row is None:
            return 0, 0
        return int(row["p50"] or 0), int(row["p95"] or 0)

    async def prune_ai_decisions(self, *, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        async with self._write_transaction() as db:
            cursor = await db.execute("DELETE FROM ai_decisions WHERE created_at < ?", (cutoff,))
        return cursor.rowcount

    async def record_ai_learning(
        self,
        chat_id: int,
        category: str,
        evidence: tuple[str, ...],
        confidence: float,
    ) -> AILearningMemory:
        normalized_category = category.strip() if isinstance(category, str) else ""
        if (
            not normalized_category
            or len(normalized_category) > 64
            or any(
                not (character.isascii() and (character.islower() or character.isdigit()))
                and character != "_"
                for character in normalized_category
            )
        ):
            raise ValueError("无效的 AI 学习分类")
        if not evidence or any(
            not isinstance(item, str) or item not in AI_LEARNING_EVIDENCE_SET for item in evidence
        ):
            raise ValueError("AI 学习证据必须使用固定枚举且不能为空")
        evidence_set = frozenset(evidence)
        evidence_profile = ",".join(
            item for item in AI_LEARNING_EVIDENCE_TYPES if item in evidence_set
        )
        if isinstance(confidence, bool):
            raise ValueError("AI 学习置信度必须在 0 到 1 之间")
        try:
            normalized_confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("AI 学习置信度必须在 0 到 1 之间") from exc
        if not math.isfinite(normalized_confidence) or not 0 <= normalized_confidence <= 1:
            raise ValueError("AI 学习置信度必须在 0 到 1 之间")

        timestamp = utc_now()
        async with self._write_transaction() as db:
            await db.execute(
                """
                INSERT INTO ai_learning_memory (
                    chat_id, category, evidence_profile, decision_count,
                    max_confidence, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(chat_id, category, evidence_profile) DO UPDATE SET
                    decision_count = ai_learning_memory.decision_count + 1,
                    max_confidence = MAX(
                        ai_learning_memory.max_confidence,
                        excluded.max_confidence
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    normalized_category,
                    evidence_profile,
                    normalized_confidence,
                    timestamp,
                    timestamp,
                ),
            )
            cursor = await db.execute(
                """
                SELECT chat_id, category, evidence_profile, decision_count,
                       max_confidence, created_at, updated_at
                FROM ai_learning_memory
                WHERE chat_id = ? AND category = ? AND evidence_profile = ?
                """,
                (chat_id, normalized_category, evidence_profile),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("AI 学习记忆写入后读取失败")
        return self._learning_memory_from_row(row)

    async def ai_learning_memory(self, chat_id: int) -> tuple[AILearningMemory, ...]:
        cursor = await self.db.execute(
            """
            SELECT chat_id, category, evidence_profile, decision_count,
                   max_confidence, created_at, updated_at
            FROM ai_learning_memory
            WHERE chat_id = ?
            ORDER BY category, evidence_profile
            """,
            (chat_id,),
        )
        return tuple(self._learning_memory_from_row(row) for row in await cursor.fetchall())

    @staticmethod
    def _learning_memory_from_row(row: aiosqlite.Row) -> AILearningMemory:
        return AILearningMemory(
            chat_id=int(row["chat_id"]),
            category=str(row["category"]),
            evidence_profile=str(row["evidence_profile"]),
            decision_count=int(row["decision_count"]),
            max_confidence=float(row["max_confidence"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def upsert_ai_signature(
        self,
        *,
        chat_id: int,
        normalized_signature: str,
        display_signature: str,
        category: str,
        status: str,
        confidence: float,
    ) -> AISignature:
        if status not in {"candidate", "active", "rejected", "disabled"}:
            raise ValueError("无效的 AI 特征状态")
        timestamp = utc_now()
        async with self._write_transaction() as db:
            await db.execute(
                """
                INSERT INTO ai_signatures (
                    chat_id, normalized_signature, display_signature, category,
                    status, max_confidence, hit_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(chat_id, normalized_signature) DO UPDATE SET
                    display_signature = excluded.display_signature,
                    category = excluded.category,
                    status = CASE
                        WHEN ai_signatures.status = 'disabled' THEN 'disabled'
                        WHEN ai_signatures.status = 'active' THEN 'active'
                        WHEN excluded.status = 'active' THEN 'active'
                        WHEN ai_signatures.status = 'candidate'
                             AND excluded.status = 'rejected' THEN 'candidate'
                        ELSE excluded.status
                    END,
                    max_confidence = MAX(ai_signatures.max_confidence, excluded.max_confidence),
                    hit_count = ai_signatures.hit_count + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    normalized_signature,
                    display_signature,
                    category,
                    status,
                    confidence,
                    timestamp,
                    timestamp,
                ),
            )
            cursor = await db.execute(
                """
                SELECT id, normalized_signature, display_signature, category, status,
                       max_confidence, hit_count, created_at, updated_at
                FROM ai_signatures
                WHERE chat_id = ? AND normalized_signature = ?
                """,
                (chat_id, normalized_signature),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("AI 特征写入后读取失败")
        return self._signature_from_row(row)

    async def ai_signatures(
        self, chat_id: int, *, statuses: tuple[str, ...] = ("active",)
    ) -> tuple[AISignature, ...]:
        if not statuses:
            return ()
        placeholders = ",".join("?" for _ in statuses)
        cursor = await self.db.execute(
            f"""
            SELECT id, normalized_signature, display_signature, category, status,
                   max_confidence, hit_count, created_at, updated_at
            FROM ai_signatures
            WHERE chat_id = ? AND status IN ({placeholders})
            ORDER BY id
            """,  # noqa: S608 - placeholders are generated from a fixed-length tuple
            (chat_id, *statuses),
        )
        return tuple(self._signature_from_row(row) for row in await cursor.fetchall())

    async def active_ai_signatures(self, chat_id: int) -> set[str]:
        return {
            signature.normalized_signature
            for signature in await self.ai_signatures(chat_id, statuses=("active",))
        }

    async def record_ai_signature_hit(self, chat_id: int, normalized_signature: str) -> bool:
        async with self._write_transaction() as db:
            cursor = await db.execute(
                """
                UPDATE ai_signatures
                SET hit_count = hit_count + 1, updated_at = ?
                WHERE chat_id = ? AND normalized_signature = ? AND status = 'active'
                """,
                (utc_now(), chat_id, normalized_signature),
            )
        return cursor.rowcount > 0

    async def activate_ai_signature(self, chat_id: int, signature_id: int) -> bool:
        async with self._write_transaction() as db:
            cursor = await db.execute(
                """
                UPDATE ai_signatures SET status = 'active', updated_at = ?
                WHERE chat_id = ? AND id = ? AND status = 'candidate'
                """,
                (utc_now(), chat_id, signature_id),
            )
        return cursor.rowcount > 0

    async def disable_ai_signature(self, chat_id: int, identifier: str) -> bool:
        normalized_identifier = identifier.strip()
        async with self._write_transaction() as db:
            if normalized_identifier.isdigit():
                cursor = await db.execute(
                    """
                    UPDATE ai_signatures SET status = 'disabled', updated_at = ?
                    WHERE chat_id = ? AND id = ? AND status <> 'disabled'
                    """,
                    (utc_now(), chat_id, int(normalized_identifier)),
                )
            else:
                cursor = await db.execute(
                    """
                    UPDATE ai_signatures SET status = 'disabled', updated_at = ?
                    WHERE chat_id = ? AND normalized_signature = ? AND status <> 'disabled'
                    """,
                    (utc_now(), chat_id, normalized_identifier),
                )
        return cursor.rowcount > 0

    @staticmethod
    def _signature_from_row(row: aiosqlite.Row) -> AISignature:
        return AISignature(
            id=int(row["id"]),
            normalized_signature=str(row["normalized_signature"]),
            display_signature=str(row["display_signature"]),
            category=str(row["category"]),
            status=str(row["status"]),
            max_confidence=float(row["max_confidence"]),
            hit_count=int(row["hit_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def set_runtime_setting(self, chat_id: int, key: str, value: str) -> None:
        async with self._write_transaction() as db:
            await db.execute(
                """
                INSERT INTO runtime_settings (chat_id, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (chat_id, key, value, utc_now()),
            )

    async def runtime_setting(self, chat_id: int, key: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT value FROM runtime_settings WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        )
        row = await cursor.fetchone()
        return str(row["value"]) if row is not None else None

    async def delete_runtime_setting(self, chat_id: int, key: str) -> None:
        async with self._write_transaction() as db:
            await db.execute(
                "DELETE FROM runtime_settings WHERE chat_id = ? AND key = ?",
                (chat_id, key),
            )

    async def enqueue_ephemeral(self, chat_id: int, message_id: int, delete_at: str) -> None:
        async with self._write_transaction() as db:
            await db.execute(
                """
                INSERT INTO ephemeral_bot_messages (chat_id, message_id, delete_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO NOTHING
                """,
                (chat_id, message_id, delete_at),
            )

    async def due_ephemeral(self, now: str, limit: int = 100) -> tuple[EphemeralRecord, ...]:
        cursor = await self.db.execute(
            """
            SELECT chat_id, message_id, delete_at
            FROM ephemeral_bot_messages
            WHERE delete_at <= ?
            ORDER BY delete_at
            LIMIT ?
            """,
            (now, limit),
        )
        return tuple(
            EphemeralRecord(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                delete_at=str(row["delete_at"]),
            )
            for row in await cursor.fetchall()
        )

    async def queue_health(self, now: str) -> QueueHealth:
        cursor = await self.db.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM ephemeral_bot_messages) AS ephemeral_total,
                (SELECT COUNT(*) FROM ephemeral_bot_messages
                 WHERE delete_at <= ?) AS ephemeral_due,
                (SELECT COUNT(*) FROM log_outbox) AS log_outbox_total,
                (SELECT COUNT(*) FROM log_outbox
                 WHERE due_at <= ?
                   AND (claimed_until IS NULL OR claimed_until <= ?)) AS log_outbox_due,
                (SELECT COUNT(*) FROM log_outbox
                 WHERE attempt_count > 0) AS log_outbox_retrying
            """,
            (now, now, now),
        )
        row = await cursor.fetchone()
        if row is None:
            return QueueHealth(0, 0, 0, 0, 0)
        return QueueHealth(
            ephemeral_total=int(row["ephemeral_total"]),
            ephemeral_due=int(row["ephemeral_due"]),
            log_outbox_total=int(row["log_outbox_total"]),
            log_outbox_due=int(row["log_outbox_due"]),
            log_outbox_retrying=int(row["log_outbox_retrying"]),
        )

    async def ack_ephemeral(self, chat_id: int, message_id: int) -> None:
        async with self._write_transaction() as db:
            await db.execute(
                "DELETE FROM ephemeral_bot_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )

    async def reschedule_ephemeral(self, chat_id: int, message_id: int, delete_at: str) -> None:
        async with self._write_transaction() as db:
            await db.execute(
                """
                UPDATE ephemeral_bot_messages
                SET delete_at = ?
                WHERE chat_id = ? AND message_id = ?
                """,
                (delete_at, chat_id, message_id),
            )

    async def claim_due_log_outbox(
        self,
        now: str,
        *,
        lease_until: str,
        limit: int = 100,
    ) -> tuple[LogOutboxRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        async with self._write_transaction() as db:
            cursor = await db.execute(
                """
                SELECT id
                FROM log_outbox
                WHERE due_at <= ?
                  AND (claimed_until IS NULL OR claimed_until <= ?)
                ORDER BY due_at, id
                LIMIT ?
                """,
                (now, now, limit),
            )
            outbox_ids = tuple(int(row["id"]) for row in await cursor.fetchall())
            if not outbox_ids:
                return ()
            placeholders = ",".join("?" for _ in outbox_ids)
            await db.execute(
                f"""
                UPDATE log_outbox
                SET claimed_until = ?
                WHERE id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated from selected integer ids
                (lease_until, *outbox_ids),
            )
            claimed_cursor = await db.execute(
                f"""
                SELECT id, chat_id, message_id, actor_id, target_id, action,
                       due_at, attempt_count, created_at
                FROM log_outbox
                WHERE id IN ({placeholders})
                ORDER BY due_at, id
                """,  # noqa: S608 - placeholders are generated from selected integer ids
                outbox_ids,
            )
            rows = await claimed_cursor.fetchall()
        return tuple(self._log_outbox_from_row(row) for row in rows)

    async def ack_log_outbox(self, outbox_id: int) -> None:
        async with self._write_transaction() as db:
            await db.execute("DELETE FROM log_outbox WHERE id = ?", (outbox_id,))

    async def reschedule_log_outbox(self, outbox_id: int, due_at: str) -> None:
        async with self._write_transaction() as db:
            await db.execute(
                """
                UPDATE log_outbox
                SET due_at = ?, claimed_until = NULL, attempt_count = attempt_count + 1
                WHERE id = ?
                """,
                (due_at, outbox_id),
            )

    @staticmethod
    def _log_outbox_from_row(row: aiosqlite.Row) -> LogOutboxRecord:
        return LogOutboxRecord(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            message_id=(int(row["message_id"]) if row["message_id"] is not None else None),
            actor_id=int(row["actor_id"]) if row["actor_id"] is not None else None,
            target_id=int(row["target_id"]) if row["target_id"] is not None else None,
            action=str(row["action"]),
            due_at=str(row["due_at"]),
            attempt_count=int(row["attempt_count"]),
            created_at=str(row["created_at"]),
        )
