import sqlite3
from pathlib import Path

import pytest

from nexa_guardian.storage import SCHEMA_VERSION, Storage


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


async def test_warning_increment_and_clear(storage: Storage) -> None:
    assert await storage.warning_count(-100, 42) == 0
    assert await storage.add_warning(-100, 42) == 1
    assert await storage.add_warning(-100, 42) == 2
    assert await storage.warning_count(-100, 42) == 2
    await storage.clear_warnings(-100, 42)
    assert await storage.warning_count(-100, 42) == 0


async def test_trusted_users_are_scoped_per_chat(storage: Storage) -> None:
    await storage.add_trusted_user(-100, 42, 7)
    assert await storage.is_trusted(-100, 42)
    assert not await storage.is_trusted(-200, 42)
    await storage.remove_trusted_user(-100, 42)
    assert not await storage.is_trusted(-100, 42)


async def test_domains_and_words_are_scoped_per_chat(storage: Storage) -> None:
    await storage.add_allowed_domain(-100, "example.com", 7)
    await storage.add_blocked_word(-100, "加微信", 7)
    assert await storage.allowed_domains(-100) == {"example.com"}
    assert await storage.blocked_words(-100) == {"加微信"}
    assert await storage.allowed_domains(-200) == set()
    await storage.remove_allowed_domain(-100, "example.com")
    await storage.remove_blocked_word(-100, "加微信")
    assert await storage.allowed_domains(-100) == set()
    assert await storage.blocked_words(-100) == set()


async def test_batch_words_are_written_and_removed_together(storage: Storage) -> None:
    await storage.add_blocked_words(-100, ("词语一", "guaranteed profit"), 7)
    assert await storage.blocked_words(-100) == {"词语一", "guaranteed profit"}
    await storage.remove_blocked_words(-100, ("词语一", "guaranteed profit"))
    assert await storage.blocked_words(-100) == set()


async def test_action_stats(storage: Storage) -> None:
    await storage.record_action(chat_id=-100, action="delete")
    await storage.record_action(chat_id=-100, action="warn")
    await storage.record_action(chat_id=-100, action="mute")
    await storage.record_action(chat_id=-100, action="auto_mute")
    await storage.record_action(chat_id=-100, action="ban")
    await storage.record_action(chat_id=-100, action="auto_ban")
    await storage.record_action(chat_id=-200, action="delete")
    stats = await storage.chat_stats(-100)
    assert stats.deleted == 1
    assert stats.warned == 1
    assert stats.muted == 2
    assert stats.banned == 2
    assert stats.total_actions == 6
    row = await storage.db.execute_fetchall(
        "SELECT reason FROM moderation_log WHERE chat_id = ?",
        (-100,),
    )
    assert {item["reason"] for item in row} == {""}


async def test_time_filtered_stats_warning_totals_and_recent_actions(storage: Storage) -> None:
    await storage.record_action(chat_id=-100, action="delete", target_id=41)
    await storage.record_action(chat_id=-100, action="warn", actor_id=7, target_id=42)
    await storage.db.execute(
        "UPDATE moderation_log SET created_at = ? WHERE target_id = ?",
        ("2026-08-20T00:00:00+00:00", 41),
    )
    await storage.db.commit()
    await storage.add_warning(-100, 42)
    await storage.add_warning(-100, 42)
    await storage.add_warning(-100, 43)

    recent_stats = await storage.chat_stats(-100, since="2026-08-26T00:00:00+00:00")
    warning_stats = await storage.warning_stats(-100)
    actions = await storage.recent_actions(-100, limit=1)

    assert recent_stats.deleted == 0
    assert recent_stats.warned == 1
    assert warning_stats.users == 2
    assert warning_stats.total == 3
    assert len(actions) == 1
    assert actions[0].action == "warn"
    assert actions[0].actor_id == 7
    assert actions[0].target_id == 42


async def test_ai_schema_decisions_and_signatures(storage: Storage) -> None:
    version = await storage.db.execute_fetchall("PRAGMA user_version")
    assert version[0][0] == SCHEMA_VERSION

    await storage.record_ai_decision(
        chat_id=-100,
        message_id=10,
        target_id=42,
        is_ad=True,
        confidence=0.98,
        category="crypto_ad",
        model="gpt-5.6",
        latency_ms=321,
        cached=False,
        enforced=True,
    )
    stats = await storage.ai_stats(-100)
    assert (stats.total, stats.ads, stats.enforced, stats.failed) == (1, 1, 1, 0)
    assert stats.calls == 1
    recent = await storage.recent_ai_decisions(-100, limit=1)
    assert recent[0].is_ad is True
    assert recent[0].category == "crypto_ad"

    created = await storage.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="免费领取usdt",
        display_signature="免费领取 USDT",
        category="crypto_ad",
        status="active",
        confidence=0.99,
    )
    assert created.status == "active"
    assert await storage.active_ai_signatures(-100) == {"免费领取usdt"}
    await storage.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="免费领取usdt",
        display_signature="免费领取 USDT",
        category="crypto_ad",
        status="candidate",
        confidence=0.94,
    )
    assert (await storage.ai_signatures(-100))[0].status == "active"
    assert await storage.record_ai_signature_hit(-100, "免费领取usdt")
    assert (await storage.ai_signatures(-100))[0].hit_count == 3
    assert await storage.disable_ai_signature(-100, str(created.id))
    assert await storage.active_ai_signatures(-100) == set()


async def test_ai_stats_distinguish_gateway_calls_cache_and_local_signatures(
    storage: Storage,
) -> None:
    records = (
        ("gpt-5.6", False, True, "", 120),
        ("gpt-5.6", True, True, "", 0),
        ("local-signature", True, True, "", 0),
        ("gpt-5.6", False, None, "AIReviewTimeout", 0),
        ("local-media", True, None, "MediaReviewError", 0),
    )
    for message_id, (model, cached, is_ad, error_type, latency_ms) in enumerate(records, 1):
        await storage.record_ai_decision(
            chat_id=-100,
            message_id=message_id,
            target_id=42,
            is_ad=is_ad,
            confidence=0.99 if is_ad is not None else None,
            category="promotion" if is_ad else "",
            model=model,
            latency_ms=latency_ms,
            cached=cached,
            enforced=bool(is_ad),
            error_type=error_type,
        )

    stats = await storage.ai_stats(-100)

    assert stats.total == 5
    assert stats.calls == 2
    assert stats.ads == 3
    assert stats.failed == 1
    assert stats.cached == 1
    assert stats.average_latency_ms == 120

    await storage.db.execute("UPDATE ai_decisions SET created_at = '2000-01-01T00:00:00+00:00'")
    await storage.db.commit()
    assert await storage.prune_ai_decisions(retention_days=30) == 5
    assert (await storage.ai_stats(-100)).total == 5
    assert (await storage.ai_stats(-100, since="2026-01-01T00:00:00+00:00")).total == 0


async def test_runtime_and_ephemeral_queue(storage: Storage) -> None:
    await storage.set_runtime_setting(-100, "ai_paused", "1")
    assert await storage.runtime_setting(-100, "ai_paused") == "1"

    await storage.enqueue_ephemeral(-100, 20, "2026-08-29T00:00:00+00:00")
    due = await storage.due_ephemeral("2026-08-29T00:00:01+00:00")
    assert [(item.chat_id, item.message_id) for item in due] == [(-100, 20)]
    await storage.reschedule_ephemeral(-100, 20, "2026-08-29T00:01:00+00:00")
    assert await storage.due_ephemeral("2026-08-29T00:00:30+00:00") == ()
    await storage.ack_ephemeral(-100, 20)
    assert await storage.due_ephemeral("2026-08-29T01:00:00+00:00") == ()


async def test_ephemeral_queue_migration_removes_persisted_attempts(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v2.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE ephemeral_bot_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, message_id)
            );
            INSERT INTO ephemeral_bot_messages VALUES (-100, 20, '2026-08-29T00:00:00+00:00', 2);
            PRAGMA user_version=2;
            """
        )

    migrated = Storage(database)
    await migrated.open()
    try:
        columns = await migrated.db.execute_fetchall("PRAGMA table_info(ephemeral_bot_messages)")
        due = await migrated.due_ephemeral("2026-08-29T00:00:01+00:00")
        version = await migrated.db.execute_fetchall("PRAGMA user_version")
    finally:
        await migrated.close()

    assert [row["name"] for row in columns] == ["chat_id", "message_id", "delete_at"]
    assert [(item.chat_id, item.message_id) for item in due] == [(-100, 20)]
    assert version[0][0] == SCHEMA_VERSION


async def test_schema_v3_backfills_cumulative_ai_totals(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v3.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE ai_decisions (
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
            INSERT INTO ai_decisions (
                chat_id, is_ad, confidence, category, model, latency_ms,
                cached, enforced, error_type, created_at
            ) VALUES
                (-100, 1, 0.98, 'promotion', 'gpt-5.6', 80, 0, 1, '',
                 '2026-08-01T00:00:00+00:00'),
                (-100, NULL, NULL, '', 'gpt-5.6', 0, 0, 0, 'AIReviewTimeout',
                 '2026-08-01T00:01:00+00:00');
            PRAGMA user_version=3;
            """
        )

    migrated = Storage(database)
    await migrated.open()
    try:
        stats = await migrated.ai_stats(-100)
        version = await migrated.db.execute_fetchall("PRAGMA user_version")
    finally:
        await migrated.close()

    assert stats.total == 2
    assert stats.calls == 2
    assert stats.ads == 1
    assert stats.failed == 1
    assert stats.average_latency_ms == 80
    assert version[0][0] == SCHEMA_VERSION
