from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from nexa_guardian.ai_contract import AI_GATEWAY_MODELS

SCHEMA_VERSION = 5
UTC = timezone.utc

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


@dataclass(frozen=True, slots=True)
class AIStats:
    total: int
    calls: int
    ads: int
    enforced: int
    failed: int
    cached: int
    average_latency_ms: int


@dataclass(frozen=True, slots=True)
class AIDecision:
    is_ad: bool | None
    confidence: float | None
    category: str
    model: str
    latency_ms: int
    cached: bool
    enforced: bool
    error_type: str
    created_at: str


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
        cursor = await self._db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        current_version = int(row[0]) if row is not None else 0
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(f"数据库版本 {current_version} 高于程序支持的 {SCHEMA_VERSION}")
        await self._db.executescript(SCHEMA)
        await self._ensure_ephemeral_schema()
        if current_version < 4:
            await self._rebuild_ai_totals()
        await self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._db.commit()

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
            """
            INSERT INTO ai_totals (
                chat_id, decisions, calls, ads, enforced, failed, cached,
                latency_total_ms, latency_samples
            )
            SELECT
                chat_id,
                COUNT(*),
                SUM(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol') AND cached = 0 THEN 1 ELSE 0
                END),
                SUM(CASE WHEN is_ad = 1 THEN 1 ELSE 0 END),
                SUM(enforced),
                SUM(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol') AND error_type <> '' THEN 1 ELSE 0
                END),
                SUM(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol') AND cached = 1 THEN 1 ELSE 0
                END),
                SUM(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol')
                         AND cached = 0 AND error_type = ''
                    THEN latency_ms ELSE 0
                END),
                SUM(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol')
                         AND cached = 0 AND error_type = ''
                    THEN 1 ELSE 0
                END)
            FROM ai_decisions
            GROUP BY chat_id
            """
        )

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
        if row is None:
            raise RuntimeError("警告写入后读取失败")
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

    async def add_blocked_words(self, chat_id: int, words: tuple[str, ...], actor_id: int) -> None:
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
                SUM(CASE WHEN action IN ('ban', 'auto_ban') THEN 1 ELSE 0 END) AS banned,
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
        if row is None:
            return ChatStats(0, 0, 0, 0, 0)
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
        if row is None:
            return WarningStats(0, 0)
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
    ) -> None:
        gateway_call = int(model in AI_GATEWAY_MODELS and not cached)
        gateway_cache = int(model in AI_GATEWAY_MODELS and cached)
        gateway_failure = int(model in AI_GATEWAY_MODELS and bool(error_type))
        latency_sample = int(gateway_call == 1 and not error_type)
        safe_latency_ms = max(0, latency_ms)
        async with self._write_lock:
            try:
                await self.db.execute(
                    """
                    INSERT INTO ai_decisions (
                        chat_id, message_id, target_id, is_ad, confidence, category,
                        model, latency_ms, cached, enforced, error_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        utc_now(),
                    ),
                )
                await self.db.execute(
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
            except BaseException:
                await self.db.rollback()
                raise
            else:
                await self.db.commit()

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

        query = """
            SELECT
                COUNT(*) AS total,
                SUM(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol') AND cached = 0 THEN 1 ELSE 0
                END) AS calls,
                SUM(CASE WHEN is_ad = 1 THEN 1 ELSE 0 END) AS ads,
                SUM(enforced) AS enforced,
                SUM(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol') AND error_type <> '' THEN 1 ELSE 0
                END) AS failed,
                SUM(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol') AND cached = 1 THEN 1 ELSE 0
                END) AS cached,
                AVG(CASE
                    WHEN model IN ('gpt-5.6', 'gpt-5.6-sol')
                         AND cached = 0 AND error_type = ''
                    THEN latency_ms
                END) AS average_latency_ms
            FROM ai_decisions
            WHERE chat_id = ?
        """
        query += " AND created_at >= ?"
        cursor = await self.db.execute(query, (chat_id, since))
        row = await cursor.fetchone()
        if row is None:
            return AIStats(0, 0, 0, 0, 0, 0, 0)
        return AIStats(
            total=int(row["total"] or 0),
            calls=int(row["calls"] or 0),
            ads=int(row["ads"] or 0),
            enforced=int(row["enforced"] or 0),
            failed=int(row["failed"] or 0),
            cached=int(row["cached"] or 0),
            average_latency_ms=int(row["average_latency_ms"] or 0),
        )

    async def recent_ai_decisions(self, chat_id: int, *, limit: int) -> tuple[AIDecision, ...]:
        cursor = await self.db.execute(
            """
            SELECT is_ad, confidence, category, model, latency_ms, cached,
                   enforced, error_type, created_at
            FROM ai_decisions
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        return tuple(
            AIDecision(
                is_ad=None if row["is_ad"] is None else bool(row["is_ad"]),
                confidence=(float(row["confidence"]) if row["confidence"] is not None else None),
                category=str(row["category"]),
                model=str(row["model"]),
                latency_ms=int(row["latency_ms"]),
                cached=bool(row["cached"]),
                enforced=bool(row["enforced"]),
                error_type=str(row["error_type"]),
                created_at=str(row["created_at"]),
            )
            for row in await cursor.fetchall()
        )

    async def prune_ai_decisions(self, *, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        async with self._write_lock:
            cursor = await self.db.execute(
                "DELETE FROM ai_decisions WHERE created_at < ?", (cutoff,)
            )
            await self.db.commit()
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
        async with self._write_lock:
            try:
                await self.db.execute(
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
                cursor = await self.db.execute(
                    """
                    SELECT chat_id, category, evidence_profile, decision_count,
                           max_confidence, created_at, updated_at
                    FROM ai_learning_memory
                    WHERE chat_id = ? AND category = ? AND evidence_profile = ?
                    """,
                    (chat_id, normalized_category, evidence_profile),
                )
                row = await cursor.fetchone()
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
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
        async with self._write_lock:
            await self.db.execute(
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
            cursor = await self.db.execute(
                """
                SELECT id, normalized_signature, display_signature, category, status,
                       max_confidence, hit_count, created_at, updated_at
                FROM ai_signatures
                WHERE chat_id = ? AND normalized_signature = ?
                """,
                (chat_id, normalized_signature),
            )
            row = await cursor.fetchone()
            await self.db.commit()
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
        async with self._write_lock:
            cursor = await self.db.execute(
                """
                UPDATE ai_signatures
                SET hit_count = hit_count + 1, updated_at = ?
                WHERE chat_id = ? AND normalized_signature = ? AND status = 'active'
                """,
                (utc_now(), chat_id, normalized_signature),
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def activate_ai_signature(self, chat_id: int, signature_id: int) -> bool:
        async with self._write_lock:
            cursor = await self.db.execute(
                """
                UPDATE ai_signatures SET status = 'active', updated_at = ?
                WHERE chat_id = ? AND id = ? AND status = 'candidate'
                """,
                (utc_now(), chat_id, signature_id),
            )
            await self.db.commit()
        return cursor.rowcount > 0

    async def disable_ai_signature(self, chat_id: int, identifier: str) -> bool:
        normalized_identifier = identifier.strip()
        async with self._write_lock:
            if normalized_identifier.isdigit():
                cursor = await self.db.execute(
                    """
                    UPDATE ai_signatures SET status = 'disabled', updated_at = ?
                    WHERE chat_id = ? AND id = ? AND status <> 'disabled'
                    """,
                    (utc_now(), chat_id, int(normalized_identifier)),
                )
            else:
                cursor = await self.db.execute(
                    """
                    UPDATE ai_signatures SET status = 'disabled', updated_at = ?
                    WHERE chat_id = ? AND normalized_signature = ? AND status <> 'disabled'
                    """,
                    (utc_now(), chat_id, normalized_identifier),
                )
            await self.db.commit()
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
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO runtime_settings (chat_id, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (chat_id, key, value, utc_now()),
            )
            await self.db.commit()

    async def runtime_setting(self, chat_id: int, key: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT value FROM runtime_settings WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        )
        row = await cursor.fetchone()
        return str(row["value"]) if row is not None else None

    async def enqueue_ephemeral(self, chat_id: int, message_id: int, delete_at: str) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                INSERT INTO ephemeral_bot_messages (chat_id, message_id, delete_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO NOTHING
                """,
                (chat_id, message_id, delete_at),
            )
            await self.db.commit()

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

    async def ack_ephemeral(self, chat_id: int, message_id: int) -> None:
        async with self._write_lock:
            await self.db.execute(
                "DELETE FROM ephemeral_bot_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            await self.db.commit()

    async def reschedule_ephemeral(self, chat_id: int, message_id: int, delete_at: str) -> None:
        async with self._write_lock:
            await self.db.execute(
                """
                UPDATE ephemeral_bot_messages
                SET delete_at = ?
                WHERE chat_id = ? AND message_id = ?
                """,
                (delete_at, chat_id, message_id),
            )
            await self.db.commit()
