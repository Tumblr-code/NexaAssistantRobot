import asyncio
import sqlite3
from pathlib import Path

import pytest

from nexa_guardian.ai_contract import AI_MODEL
from nexa_guardian.storage import SCHEMA_VERSION, Storage, normalize_raffle_keyword


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


async def test_warning_increment_and_clear(storage: Storage) -> None:
    empty = await storage.warning_stats(-100)
    assert (empty.users, empty.total) == (0, 0)
    assert await storage.add_warning_and_record_action(-100, 42) == 1
    assert await storage.add_warning_and_record_action(-100, 42) == 2
    assert (await storage.warning_stats(-100)).total == 2
    await storage.clear_warnings(-100, 42)
    assert (await storage.warning_stats(-100)).total == 0


async def test_warning_stats_ignore_legacy_zero_count_rows(storage: Storage) -> None:
    await storage.db.execute(
        """
        INSERT INTO warnings (chat_id, user_id, count, last_reason, updated_at)
        VALUES (-100, 42, 0, '', '2026-08-31T00:00:00+00:00')
        """
    )
    await storage.db.commit()

    stats = await storage.warning_stats(-100)

    assert (stats.users, stats.total) == (0, 0)


async def test_raffle_lifecycle_is_persistent_and_idempotent(storage: Storage) -> None:
    raffle = await storage.create_raffle(
        chat_id=-100,
        creator_id=7,
        keyword="参与",
        prize="GiftPass 月卡",
        winner_count=2,
        ends_at="2099-01-01T00:00:00+00:00",
    )
    assert await storage.set_raffle_message(raffle.id, 88)
    assert await storage.activate_raffle(raffle.id)
    assert (await storage.active_raffle(-100)).message_id == 88  # type: ignore[union-attr]
    assert await storage.replace_active_raffle_message(raffle.id, 88, 89)
    assert not await storage.replace_active_raffle_message(raffle.id, 88, 90)
    assert (await storage.active_raffle(-100)).message_id == 89  # type: ignore[union-attr]

    assert await storage.join_raffle(raffle.id, 41) == (True, 1)
    assert await storage.join_raffle(raffle.id, 41) == (False, 1)
    assert await storage.join_raffle(raffle.id, 42) == (True, 2)
    assert await storage.raffle_entry_ids(raffle.id) == (41, 42)

    assert await storage.finish_raffle(raffle.id, (42, 41))
    assert not await storage.finish_raffle(raffle.id, (41,))
    assert not await storage.replace_active_raffle_message(raffle.id, 89, 90)
    winners = await storage.raffle_winners(raffle.id)
    assert [(item.position, item.user_id) for item in winners] == [(1, 42), (2, 41)]
    assert await storage.active_raffle(-100) is None


async def test_raffle_allows_only_one_running_per_chat_and_supports_reroll(
    storage: Storage,
) -> None:
    raffle = await storage.create_raffle(
        chat_id=-100,
        creator_id=7,
        keyword="Nexa",
        prize="ModelPass",
        winner_count=1,
        ends_at="2099-01-01T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="已有进行中的抽奖"):
        await storage.create_raffle(
            chat_id=-100,
            creator_id=7,
            keyword="第二场",
            prize="第二场",
            winner_count=1,
            ends_at="2099-01-01T00:00:00+00:00",
        )
    assert await storage.set_raffle_message(raffle.id, 88)
    assert await storage.activate_raffle(raffle.id)
    await storage.join_raffle(raffle.id, 41)
    assert await storage.finish_raffle(raffle.id, (41,))
    assert (
        await storage.add_reroll(
            raffle.id,
            (41,),
            result_delivery_stage="group_and_admin",
        )
        == 2
    )
    assert (await storage.raffle_winners(raffle.id))[0].round == 2
    pending = await storage.pending_raffle_results(chat_ids={-100})
    assert [(item.round_number, item.stage) for item in pending] == [(2, "group_and_admin")]


async def test_pending_raffle_results_are_sorted_by_numeric_round(storage: Storage) -> None:
    raffle = await storage.create_raffle(
        chat_id=-100,
        creator_id=7,
        keyword="参与",
        prize="GiftPass",
        winner_count=1,
        ends_at="2099-01-01T00:00:00+00:00",
    )
    assert await storage.set_raffle_message(raffle.id, 88)
    assert await storage.activate_raffle(raffle.id)
    assert await storage.finish_raffle(raffle.id, ())
    await storage.set_runtime_setting(
        -100,
        f"raffle_result_pending:{raffle.id}:10",
        "group_only",
    )
    await storage.set_runtime_setting(
        -100,
        f"raffle_result_pending:{raffle.id}:2",
        "group_only",
    )

    pending = await storage.pending_raffle_results(chat_ids={-100})

    assert [item.round_number for item in pending] == [2, 10]


def test_raffle_keyword_normalization_is_exact_and_rejects_unsafe_values() -> None:
    assert normalize_raffle_keyword("  ＮＥＸＡ\u200b  ") == "nexa"
    assert normalize_raffle_keyword("参与") == "参与"
    with pytest.raises(ValueError, match="不能包含空格"):
        normalize_raffle_keyword("Nexa 参与")
    with pytest.raises(ValueError, match="命令、账号或链接"):
        normalize_raffle_keyword("@Nexa")


async def test_pending_raffle_is_not_joinable_until_activated(storage: Storage) -> None:
    raffle = await storage.create_raffle(
        chat_id=-100,
        creator_id=7,
        keyword="参与",
        prize="GiftPass",
        winner_count=1,
        ends_at="2099-01-01T00:00:00+00:00",
    )

    assert raffle.publication_state == "pending"
    assert await storage.active_raffle(-100) is None
    assert await storage.join_raffle(raffle.id, 41) == (False, 0)
    assert await storage.set_raffle_message(raffle.id, 88)
    assert await storage.activate_raffle(raffle.id)
    active = await storage.active_raffle(-100)
    assert active is not None and active.id == raffle.id


async def test_raffle_work_queries_are_scoped_to_allowed_chats(storage: Storage) -> None:
    for chat_id, message_id in ((-100, 88), (-200, 99)):
        raffle = await storage.create_raffle(
            chat_id=chat_id,
            creator_id=7,
            keyword=f"join{abs(chat_id)}",
            prize="GiftPass",
            winner_count=1,
            ends_at="2026-08-30T00:00:00+00:00",
        )
        assert await storage.set_raffle_message(raffle.id, message_id)
        assert await storage.activate_raffle(raffle.id)

    due = await storage.due_raffles(
        "2026-08-31T00:00:00+00:00",
        chat_ids=frozenset({-100}),
    )

    assert [raffle.chat_id for raffle in due] == [-100]


async def test_schema_v8_migrates_finished_raffles_but_rejects_running_v7(
    tmp_path: Path,
) -> None:
    def create_v7(path: Path, status: str) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE raffles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                creator_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                winner_count INTEGER NOT NULL,
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE UNIQUE INDEX idx_raffles_one_running_per_chat
            ON raffles (chat_id) WHERE status = 'running';
            CREATE INDEX idx_raffles_due ON raffles (status, ends_at, id);
            PRAGMA user_version=7;
            """
        )
        connection.execute(
            "INSERT INTO raffles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                -100,
                88,
                7,
                "GiftPass",
                1,
                "2099-01-01T00:00:00+00:00",
                status,
                "2026-08-30T00:00:00+00:00",
                None if status == "running" else "2026-08-30T01:00:00+00:00",
            ),
        )
        connection.commit()
        connection.close()

    finished_path = tmp_path / "finished-v7.db"
    create_v7(finished_path, "finished")
    migrated = Storage(finished_path)
    await migrated.open()
    try:
        raffle = await migrated.raffle(1)
        assert raffle is not None
        assert raffle.keyword is None
        assert raffle.publication_state == "active"
        assert (await migrated.db.execute_fetchall("PRAGMA user_version"))[0][0] == 8
    finally:
        await migrated.close()

    running_path = tmp_path / "running-v7.db"
    create_v7(running_path, "running")
    blocked = Storage(running_path)
    with pytest.raises(RuntimeError, match="旧版进行中抽奖"):
        await blocked.open()
    await blocked.close()


async def test_trusted_users_are_scoped_per_chat(storage: Storage) -> None:
    await storage.db.execute(
        "INSERT INTO trusted_users VALUES (?, ?, ?, ?)",
        (-100, 42, 7, "2026-08-30T00:00:00+00:00"),
    )
    await storage.db.execute(
        "INSERT INTO trusted_users VALUES (?, ?, ?, ?)",
        (-100, 43, 7, "2026-08-30T00:00:00+00:00"),
    )
    await storage.db.commit()
    assert await storage.is_trusted(-100, 42)
    assert not await storage.is_trusted(-200, 42)
    assert await storage.trusted_user_ids(-100) == frozenset({42, 43})
    assert await storage.trusted_user_ids(-200) == frozenset()


async def test_domains_and_words_are_scoped_per_chat(storage: Storage) -> None:
    await storage.db.execute(
        "INSERT INTO allowed_domains VALUES (?, ?, ?, ?)",
        (-100, "example.com", 7, "2026-08-30T00:00:00+00:00"),
    )
    await storage.db.execute(
        "INSERT INTO blocked_words VALUES (?, ?, ?, ?)",
        (-100, "加微信", 7, "2026-08-30T00:00:00+00:00"),
    )
    await storage.db.commit()
    assert await storage.allowed_domains(-100) == {"example.com"}
    assert await storage.blocked_words(-100) == {"加微信"}
    assert await storage.allowed_domains(-200) == set()


async def test_action_stats(storage: Storage) -> None:
    actions = (
        "delete",
        "warn",
        "ban",
        "auto_ban",
        "member_cleanup",
        "cleanup_inactive",
        "kick",
        "member_cleanup_unban_failed",
        "cleanup_inactive_unban_failed",
        "kick_unban_failed",
        "history_purge",
        "mute",
    )
    for action in actions:
        await storage.record_action(chat_id=-100, action=action)
    await storage.record_action(chat_id=-200, action="delete")
    stats = await storage.chat_stats(-100)
    assert stats.deleted == 1
    assert stats.warned == 1
    assert stats.banned == 2
    assert stats.removed == 3
    assert stats.cleanup_failures == 3
    assert stats.history_purges == 1
    row = await storage.db.execute_fetchall(
        "SELECT reason FROM moderation_log WHERE chat_id = ?",
        (-100,),
    )
    assert {item["reason"] for item in row} == {""}


async def test_time_filtered_stats_and_warning_totals(storage: Storage) -> None:
    await storage.record_action(chat_id=-100, action="delete", target_id=41)
    await storage.record_action(chat_id=-100, action="warn", actor_id=7, target_id=42)
    await storage.db.execute(
        "UPDATE moderation_log SET created_at = ? WHERE target_id = ?",
        ("2026-08-20T00:00:00+00:00", 41),
    )
    await storage.db.commit()
    await storage.db.executemany(
        "INSERT INTO warnings VALUES (?, ?, ?, '', ?)",
        (
            (-100, 42, 2, "2026-08-30T00:00:00+00:00"),
            (-100, 43, 1, "2026-08-30T00:00:00+00:00"),
        ),
    )
    await storage.db.commit()

    recent_stats = await storage.chat_stats(-100, since="2026-08-26T00:00:00+00:00")
    warning_stats = await storage.warning_stats(-100)

    assert recent_stats.deleted == 0
    assert recent_stats.warned == 1
    assert warning_stats.users == 2
    assert warning_stats.total == 3


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
        model=AI_MODEL,
        latency_ms=321,
        cached=False,
        enforced=True,
        reasoning_effort="high",
        service_tier="priority",
        review_path="gateway",
        media_latency_ms=20,
        queue_latency_ms=10,
        request_latency_ms=250,
        action_latency_ms=31,
        total_latency_ms=311,
        attempt_count=1,
    )
    stats = await storage.ai_stats(-100)
    assert (stats.total, stats.ads, stats.enforced, stats.failed) == (1, 1, 1, 0)
    assert stats.calls == 1
    rows = await storage.db.execute_fetchall(
        """
        SELECT is_ad, category, reasoning_effort, service_tier, review_path,
               media_latency_ms, queue_latency_ms, request_latency_ms,
               action_latency_ms, total_latency_ms, attempt_count
        FROM ai_decisions WHERE chat_id = ?
        """,
        (-100,),
    )
    assert bool(rows[0]["is_ad"])
    assert rows[0]["category"] == "crypto_ad"
    assert rows[0]["reasoning_effort"] == "high"
    assert rows[0]["service_tier"] == "priority"
    assert rows[0]["review_path"] == "gateway"
    assert (
        rows[0]["media_latency_ms"],
        rows[0]["queue_latency_ms"],
        rows[0]["request_latency_ms"],
        rows[0]["action_latency_ms"],
        rows[0]["total_latency_ms"],
        rows[0]["attempt_count"],
    ) == (20, 10, 250, 31, 311, 1)

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

    candidate = await storage.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="guaranteed profit",
        display_signature="guaranteed profit",
        category="promotion",
        status="candidate",
        confidence=0.80,
    )
    assert not await storage.activate_ai_signature(-200, candidate.id)
    assert await storage.activate_ai_signature(-100, candidate.id)
    assert not await storage.activate_ai_signature(-100, candidate.id)
    await storage.disable_ai_signature(-100, str(candidate.id))
    assert not await storage.activate_ai_signature(-100, candidate.id)


async def test_ai_learning_memory_is_private_canonical_and_atomic(storage: Storage) -> None:
    columns = await storage.db.execute_fetchall("PRAGMA table_info(ai_learning_memory)")
    assert [row["name"] for row in columns] == [
        "chat_id",
        "category",
        "evidence_profile",
        "decision_count",
        "max_confidence",
        "created_at",
        "updated_at",
    ]

    first = await storage.record_ai_learning(
        -100,
        "promotion",
        ("promotion", "text", "link", "text"),
        0.80,
    )
    assert first.evidence_profile == "text,link,promotion"
    assert first.decision_count == 1
    assert first.max_confidence == 0.80

    await asyncio.gather(
        *(
            storage.record_ai_learning(
                -100,
                "promotion",
                ("link", "text", "promotion"),
                confidence,
            )
            for confidence in (0.81, 0.95, 0.85)
        )
    )
    await storage.record_ai_learning(-100, "external_contact", ("contact", "mention"), 0.91)
    await storage.record_ai_learning(-200, "promotion", ("text", "link"), 0.99)

    memories = await storage.ai_learning_memory(-100)
    assert [
        (item.category, item.evidence_profile, item.decision_count, item.max_confidence)
        for item in memories
    ] == [
        ("external_contact", "mention,contact", 1, 0.91),
        ("promotion", "text,link,promotion", 4, 0.95),
    ]
    assert len(await storage.ai_learning_memory(-200)) == 1


@pytest.mark.parametrize(
    ("category", "evidence", "confidence"),
    [
        ("promotion", (), 0.90),
        ("promotion", ("text", "raw message"), 0.90),
        ("promotion", ("https://evil.example",), 0.90),
        ("raw category", ("text",), 0.90),
        ("promotion", ("text",), -0.01),
        ("promotion", ("text",), 1.01),
        ("promotion", ("text",), float("nan")),
    ],
)
async def test_ai_learning_memory_rejects_non_aggregate_data(
    storage: Storage,
    category: str,
    evidence: tuple[str, ...],
    confidence: float,
) -> None:
    with pytest.raises(ValueError):
        await storage.record_ai_learning(-100, category, evidence, confidence)
    assert await storage.ai_learning_memory(-100) == ()


async def test_ai_stats_distinguish_gateway_calls_cache_and_local_signatures(
    storage: Storage,
) -> None:
    records = (
        ("gpt-5.6", False, True, "", 120),
        (AI_MODEL, False, True, "", 80),
        (AI_MODEL, True, True, "", 0),
        ("local-signature", True, True, "", 0),
        (AI_MODEL, False, None, "AIReviewTimeout", 0),
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

    assert stats.total == 6
    assert stats.calls == 3
    assert stats.ads == 4
    assert stats.failed == 1
    assert stats.cached == 1
    assert stats.average_latency_ms == 100

    recent_stats = await storage.ai_stats(-100, since="2000-01-01T00:00:00+00:00")
    assert recent_stats.calls == 3
    assert recent_stats.failed == 1
    assert recent_stats.cached == 1
    assert recent_stats.average_latency_ms == 100

    await storage.db.execute("UPDATE ai_decisions SET created_at = '2000-01-01T00:00:00+00:00'")
    await storage.db.commit()
    await storage.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="claim airdrop",
        display_signature="claim airdrop",
        category="promotion",
        status="candidate",
        confidence=0.80,
    )
    await storage.record_ai_learning(
        -100,
        "promotion",
        ("text", "link", "promotion"),
        0.80,
    )
    assert await storage.prune_ai_decisions(retention_days=30) == 6
    assert (await storage.ai_stats(-100)).total == 6
    assert (await storage.ai_stats(-100, since="2026-01-01T00:00:00+00:00")).total == 0
    persistent = await storage.ai_signatures(-100, statuses=("candidate",))
    assert [(item.normalized_signature, item.hit_count) for item in persistent] == [
        ("claim airdrop", 1)
    ]
    learning = await storage.ai_learning_memory(-100)
    assert [(item.evidence_profile, item.decision_count) for item in learning] == [
        ("text,link,promotion", 1)
    ]


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


async def test_ephemeral_queue_tracks_independent_records(storage: Storage) -> None:
    await storage.enqueue_ephemeral(-100, 1, "2026-08-29T00:00:00+00:00")
    await storage.enqueue_ephemeral(-100, 2, "2026-08-29T00:00:00+00:00")
    await storage.reschedule_ephemeral(-100, 1, "2026-08-29T00:10:00+00:00")
    await storage.ack_ephemeral(-100, 2)

    assert await storage.due_ephemeral("2026-08-29T00:05:00+00:00") == ()
    due = await storage.due_ephemeral("2026-08-29T00:15:00+00:00")
    assert [(item.chat_id, item.message_id) for item in due] == [(-100, 1)]


async def test_write_transaction_rolls_back_cancelled_commit(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_commit = storage.db.commit
    commit_attempts = 0

    async def cancel_first_commit() -> None:
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise asyncio.CancelledError
        await original_commit()

    monkeypatch.setattr(storage.db, "commit", cancel_first_commit)

    with pytest.raises(asyncio.CancelledError):
        await storage.record_action(chat_id=-100, action="cancelled")
    await storage.record_action(chat_id=-100, action="kept")

    rows = await storage.db.execute_fetchall("SELECT action FROM moderation_log ORDER BY id")
    assert [row["action"] for row in rows] == ["kept"]


async def test_warning_action_and_log_outbox_are_atomic(storage: Storage) -> None:
    await storage.db.executescript(
        """
        CREATE TRIGGER abort_warn_action
        BEFORE INSERT ON moderation_log
        WHEN NEW.action = 'warn'
        BEGIN
            SELECT RAISE(ABORT, 'test abort');
        END;
        """
    )
    await storage.db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await storage.add_warning_and_record_action(
            -100,
            42,
            7,
            enqueue_log=True,
        )
    assert (await storage.warning_stats(-100)).total == 0
    assert await storage.db.execute_fetchall("SELECT * FROM moderation_log") == []
    assert await storage.db.execute_fetchall("SELECT * FROM log_outbox") == []

    await storage.db.execute("DROP TRIGGER abort_warn_action")
    await storage.db.commit()
    count = await storage.add_warning_and_record_action(
        -100,
        42,
        7,
        enqueue_log=True,
        log_due_at="2026-08-30T00:00:00+00:00",
    )
    assert count == 1
    action_rows = await storage.db.execute_fetchall(
        "SELECT action, actor_id, target_id, reason FROM moderation_log"
    )
    outbox_rows = await storage.db.execute_fetchall(
        "SELECT action, actor_id, target_id, due_at FROM log_outbox"
    )
    assert [tuple(row) for row in action_rows] == [("warn", 7, 42, "")]
    assert [tuple(row) for row in outbox_rows] == [("warn", 7, 42, "2026-08-30T00:00:00+00:00")]


async def test_log_outbox_claim_lease_reschedule_and_ack(storage: Storage) -> None:
    await storage.record_action(
        chat_id=-100,
        action="ban",
        message_id=12,
        actor_id=7,
        target_id=42,
        enqueue_log=True,
        log_due_at="2026-08-30T00:00:00+00:00",
    )

    claimed = await storage.claim_due_log_outbox(
        "2026-08-30T00:01:00+00:00",
        lease_until="2026-08-30T00:02:00+00:00",
    )
    assert len(claimed) == 1
    assert (
        claimed[0].chat_id,
        claimed[0].message_id,
        claimed[0].actor_id,
        claimed[0].target_id,
        claimed[0].action,
        claimed[0].attempt_count,
    ) == (-100, 12, 7, 42, "ban", 0)
    assert (
        await storage.claim_due_log_outbox(
            "2026-08-30T00:01:30+00:00",
            lease_until="2026-08-30T00:03:00+00:00",
        )
        == ()
    )

    await storage.reschedule_log_outbox(
        claimed[0].id,
        "2026-08-30T00:04:00+00:00",
    )
    assert (
        await storage.claim_due_log_outbox(
            "2026-08-30T00:03:00+00:00",
            lease_until="2026-08-30T00:05:00+00:00",
        )
        == ()
    )
    retried = await storage.claim_due_log_outbox(
        "2026-08-30T00:04:00+00:00",
        lease_until="2026-08-30T00:05:00+00:00",
    )
    assert len(retried) == 1
    assert retried[0].attempt_count == 1
    await storage.ack_log_outbox(retried[0].id)
    assert (
        await storage.claim_due_log_outbox(
            "2026-08-31T00:00:00+00:00",
            lease_until="2026-08-31T00:01:00+00:00",
        )
        == ()
    )


async def test_queue_health_counts_due_and_retried_records(storage: Storage) -> None:
    await storage.enqueue_ephemeral(-100, 1, "2026-08-30T00:00:00+00:00")
    await storage.enqueue_ephemeral(-100, 2, "2026-08-31T00:00:00+00:00")
    await storage.record_action(
        chat_id=-100,
        action="warn",
        enqueue_log=True,
        log_due_at="2026-08-30T00:00:00+00:00",
    )
    await storage.record_action(
        chat_id=-100,
        action="ban",
        enqueue_log=True,
        log_due_at="2026-08-31T00:00:00+00:00",
    )
    claimed = await storage.claim_due_log_outbox(
        "2026-08-30T00:01:00+00:00",
        lease_until="2026-08-30T00:02:00+00:00",
    )
    await storage.reschedule_log_outbox(claimed[0].id, "2026-08-30T00:03:00+00:00")

    health = await storage.queue_health("2026-08-30T00:01:00+00:00")

    assert (
        health.ephemeral_total,
        health.ephemeral_due,
        health.log_outbox_total,
        health.log_outbox_due,
        health.log_outbox_retrying,
    ) == (2, 1, 2, 0, 1)


async def test_ai_diagnostic_counts_and_percentiles(storage: Storage) -> None:
    requests = (10, 20, 30, 40, 100)
    totals = (15, 25, 35, 45, 105)
    efforts = ("high", "high", "high", "high", "medium")
    paths = ("gateway", "gateway", "gateway", "local_signature", "cache")
    for message_id, values in enumerate(
        zip(requests, totals, efforts, paths, strict=True),
        1,
    ):
        request_latency, total_latency, effort, path = values
        await storage.record_ai_decision(
            chat_id=-100,
            message_id=message_id,
            target_id=42,
            is_ad=False,
            confidence=0.1,
            category="",
            model=AI_MODEL,
            latency_ms=request_latency,
            cached=False,
            enforced=False,
            reasoning_effort=effort,
            service_tier="priority",
            review_path=path,
            request_latency_ms=request_latency,
            total_latency_ms=total_latency,
        )

    stats = await storage.ai_stats(-100, since="2000-01-01T00:00:00+00:00")
    assert stats.reasoning_effort_counts == (("high", 4), ("medium", 1))
    assert stats.review_path_counts == (
        ("cache", 1),
        ("gateway", 3),
        ("local_signature", 1),
    )
    assert (stats.gateway_latency_p50_ms, stats.gateway_latency_p95_ms) == (30, 100)
    assert (stats.total_latency_p50_ms, stats.total_latency_p95_ms) == (35, 105)


async def test_member_activity_table_remains_for_rollback_compatibility(storage: Storage) -> None:
    columns = await storage.db.execute_fetchall("PRAGMA table_info(member_activity)")
    assert [row["name"] for row in columns] == [
        "chat_id",
        "user_id",
        "first_observed_at",
        "last_message_at",
        "last_membership_at",
        "last_status",
        "message_count",
    ]


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


async def test_schema_v5_migrates_ai_diagnostics_and_private_activity_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-v5.db"
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
                chat_id, message_id, target_id, is_ad, confidence, category,
                model, latency_ms, cached, enforced, error_type, created_at
            ) VALUES (
                -100, 10, 42, 1, 0.98, 'promotion', 'gpt-5.6-sol',
                321, 0, 1, '', '2026-08-29T00:00:00+00:00'
            );
            PRAGMA user_version=5;
            """
        )

    migrated = Storage(database)
    await migrated.open()
    try:
        columns = await migrated.db.execute_fetchall("PRAGMA table_info(ai_decisions)")
        decision_count = await migrated.db.execute_fetchall(
            "SELECT COUNT(*) AS total FROM ai_decisions WHERE chat_id = ?",
            (-100,),
        )
        activity_columns = await migrated.db.execute_fetchall("PRAGMA table_info(member_activity)")
        outbox_columns = await migrated.db.execute_fetchall("PRAGMA table_info(log_outbox)")
        version = await migrated.db.execute_fetchall("PRAGMA user_version")
    finally:
        await migrated.close()

    assert [row["name"] for row in columns][-9:] == [
        "reasoning_effort",
        "service_tier",
        "review_path",
        "media_latency_ms",
        "queue_latency_ms",
        "request_latency_ms",
        "action_latency_ms",
        "total_latency_ms",
        "attempt_count",
    ]
    assert decision_count[0]["total"] == 0
    assert [row["name"] for row in activity_columns] == [
        "chat_id",
        "user_id",
        "first_observed_at",
        "last_message_at",
        "last_membership_at",
        "last_status",
        "message_count",
    ]
    assert "reason" not in {row["name"] for row in outbox_columns}
    assert version[0][0] == SCHEMA_VERSION


async def test_schema_v3_is_safely_cleared_by_v7_migration(tmp_path: Path) -> None:
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
                 '2026-08-01T00:01:00+00:00'),
                (-100, 1, 0.96, 'promotion', 'gpt-5.6-sol', 120, 0, 1, '',
                 '2026-08-01T00:02:00+00:00');
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

    assert stats.total == 0
    assert stats.calls == 0
    assert stats.ads == 0
    assert stats.failed == 0
    assert stats.average_latency_ms == 0
    assert version[0][0] == SCHEMA_VERSION


async def test_schema_v4_migrates_to_private_ai_learning_memory(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v4.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE migration_marker (value TEXT NOT NULL);
            INSERT INTO migration_marker VALUES ('preserved');
            PRAGMA user_version=4;
            """
        )

    migrated = Storage(database)
    await migrated.open()
    try:
        columns = await migrated.db.execute_fetchall("PRAGMA table_info(ai_learning_memory)")
        marker = await migrated.db.execute_fetchall("SELECT value FROM migration_marker")
        version = await migrated.db.execute_fetchall("PRAGMA user_version")
        recorded = await migrated.record_ai_learning(
            -100,
            "promotion",
            ("text", "obfuscation"),
            0.94,
        )
    finally:
        await migrated.close()

    reopened = Storage(database)
    await reopened.open()
    try:
        persisted = await reopened.ai_learning_memory(-100)
    finally:
        await reopened.close()

    assert [row["name"] for row in columns] == [
        "chat_id",
        "category",
        "evidence_profile",
        "decision_count",
        "max_confidence",
        "created_at",
        "updated_at",
    ]
    assert marker[0]["value"] == "preserved"
    assert recorded.evidence_profile == "text,obfuscation"
    assert [(item.evidence_profile, item.decision_count) for item in persisted] == [
        ("text,obfuscation", 1)
    ]
    assert version[0][0] == SCHEMA_VERSION


async def test_schema_v7_clears_retired_history_but_preserves_learning_and_ephemeral(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-v6.db"
    legacy = Storage(database)
    await legacy.open()
    await legacy.record_ai_learning(-100, "promotion", ("link", "promotion"), 0.99)
    await legacy.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="guaranteed profit",
        display_signature="guaranteed profit",
        category="promotion",
        status="active",
        confidence=0.99,
    )
    await legacy.enqueue_ephemeral(-100, 99, "2099-01-01T00:00:00+00:00")
    await legacy.db.execute(
        "INSERT INTO warnings VALUES (?, ?, ?, '', ?)",
        (-100, 42, 1, "2026-08-30T00:00:00+00:00"),
    )
    await legacy.record_action(chat_id=-100, action="warn", target_id=42)
    await legacy.db.execute("PRAGMA user_version=6")
    await legacy.db.commit()
    await legacy.close()

    migrated = Storage(database)
    await migrated.open()
    try:
        assert len(await migrated.ai_learning_memory(-100)) == 1
        assert await migrated.active_ai_signatures(-100) == {"guaranteed profit"}
        assert len(await migrated.due_ephemeral("2100-01-01T00:00:00+00:00")) == 1
        assert (await migrated.warning_stats(-100)).total == 0
        stats = await migrated.chat_stats(-100)
        assert (
            stats.deleted,
            stats.warned,
            stats.banned,
            stats.removed,
            stats.cleanup_failures,
            stats.history_purges,
        ) == (0, 0, 0, 0, 0, 0)
    finally:
        await migrated.close()
