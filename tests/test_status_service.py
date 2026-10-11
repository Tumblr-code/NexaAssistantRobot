import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.types import ChatMemberOwner, User

from nexa_guardian.ai_contract import AI_MODEL
from nexa_guardian.config import Settings
from nexa_guardian.history_purge import SessionHealth
from nexa_guardian.status_service import StatusService, stats_cutoffs
from nexa_guardian.storage import Storage

UTC = timezone.utc
GROUP_ID = -100
ADMIN_ID = 7
BOT_ID = 99
FIXED_NOW = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return FIXED_NOW


def settings(tmp_path: Path, *, mtproto_ready: bool = True) -> Settings:
    return Settings(
        bot_token="123:ABC",  # noqa: S106 - inert test credential
        allowed_chat_ids=frozenset({GROUP_ID}),
        authorized_admin_ids=frozenset({ADMIN_ID}),
        log_chat_id=None,
        database_path=tmp_path / "guardian.db",
        allowed_domains=(),
        blocked_words=(),
        block_external_links=True,
        block_forwards=False,
        block_channel_mentions=False,
        warn_limit=3,
        notice_ttl_seconds=30,
        flood_window_seconds=8,
        flood_max_messages=6,
        duplicate_window_seconds=30,
        duplicate_max_messages=2,
        dry_run=False,
        log_level="INFO",
        openai_base_url="https://gateway.example.test",
        openai_api_key="test-key",  # noqa: S106 - inert test credential
        ai_enabled=True,
        telegram_api_id=5514593 if mtproto_ready else None,
        telegram_api_hash="test-hash" if mtproto_ready else "",
        telegram_session_path=tmp_path / "admin",
    )


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


def status_service(
    tmp_path: Path,
    storage: Storage,
    *,
    mtproto_ready: bool = True,
) -> tuple[StatusService, AsyncMock, AsyncMock]:
    bot = AsyncMock()
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Nexa", username="NexaGuardianBot")
    bot.get_me.return_value = bot_user
    bot.get_chat_member.return_value = ChatMemberOwner(
        user=bot_user,
        is_anonymous=False,
    )
    session = AsyncMock()
    session.probe.return_value = SessionHealth(
        configured=True,
        verified=True,
        detail="身份与目标群已验证",
    )
    service = StatusService(
        bot,
        settings(tmp_path, mtproto_ready=mtproto_ready),
        storage,
        session,
    )
    return service, bot, session


def test_stats_cutoffs_follow_beijing_calendar_boundaries() -> None:
    assert stats_cutoffs(FIXED_NOW) == (
        "2026-08-30T16:00:00+00:00",
        "2026-08-24T16:00:00+00:00",
    )


async def _insert_actions(storage: Storage) -> None:
    rows: list[tuple[int, None, None, None, str, str, str]] = []
    periods = (
        (
            "2026-08-30T18:00:00+00:00",
            (
                "delete",
                "warn",
                "ban",
                "member_cleanup",
                "member_cleanup_unban_failed",
                "history_purge",
            ),
        ),
        (
            "2026-08-25T00:00:00+00:00",
            ("warn", "auto_ban", "cleanup_inactive", "history_purge"),
        ),
        ("2026-08-20T00:00:00+00:00", ("delete", "ban", "kick")),
    )
    for created_at, actions in periods:
        rows.extend((GROUP_ID, None, None, None, action, "", created_at) for action in actions)
    await storage.db.executemany(
        """
        INSERT INTO moderation_log
            (chat_id, message_id, actor_id, target_id, action, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    await storage.db.executemany(
        "INSERT INTO warnings VALUES (?, ?, ?, '', ?)",
        (
            (GROUP_ID, 41, 2, "2026-08-30T18:00:00+00:00"),
            (GROUP_ID, 42, 1, "2026-08-30T18:00:00+00:00"),
        ),
    )
    await storage.db.commit()


async def _insert_ai_metrics(storage: Storage) -> None:
    records = (
        (1, True, "", 100, "2026-08-25T01:00:00+00:00"),
        (2, False, "", 200, "2026-08-26T01:00:00+00:00"),
        (3, True, "", 300, "2026-08-27T01:00:00+00:00"),
        (4, None, "AIReviewTimeout", 400, "2026-08-28T01:00:00+00:00"),
        (5, True, "AIReviewTimeout", 900, "2026-08-20T01:00:00+00:00"),
    )
    for message_id, is_ad, error_type, latency_ms, created_at in records:
        await storage.record_ai_decision(
            chat_id=GROUP_ID,
            message_id=message_id,
            target_id=42,
            is_ad=is_ad,
            confidence=0.99 if is_ad is not None else None,
            category="promotion" if is_ad else "",
            model=AI_MODEL,
            latency_ms=latency_ms,
            cached=False,
            enforced=is_ad is True,
            error_type=error_type,
            reasoning_effort="high",
            service_tier="priority",
            review_path="gateway",
            request_latency_ms=latency_ms,
            total_latency_ms=latency_ms + 10,
        )
        await storage.db.execute(
            "UPDATE ai_decisions SET created_at = ? WHERE chat_id = ? AND message_id = ?",
            (created_at, GROUP_ID, message_id),
        )
    await storage.db.commit()


async def _insert_queue_health(storage: Storage) -> None:
    await storage.enqueue_ephemeral(
        GROUP_ID,
        1001,
        "2026-08-30T23:00:00+00:00",
    )
    await storage.enqueue_ephemeral(
        GROUP_ID,
        1002,
        "2026-09-01T00:00:00+00:00",
    )
    await storage.record_action(
        chat_id=GROUP_ID,
        action="queue_probe",
        enqueue_log=True,
        log_due_at="2026-08-30T23:00:00+00:00",
    )
    await storage.record_action(
        chat_id=GROUP_ID,
        action="queue_probe",
        enqueue_log=True,
        log_due_at="2026-08-30T23:00:00+00:00",
    )
    await storage.db.execute(
        "UPDATE log_outbox SET attempt_count = 2 WHERE id = (SELECT MIN(id) FROM log_outbox)"
    )
    await storage.db.commit()


async def test_overview_reports_new_statistics_ai_and_queue_health(
    tmp_path: Path,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexa_guardian.status_service.datetime", FixedDateTime)
    await _insert_actions(storage)
    await _insert_ai_metrics(storage)
    await _insert_queue_health(storage)
    await storage.set_runtime_setting(GROUP_ID, "ai_paused", "1")
    service, _, session = status_service(tmp_path, storage)

    text = await service.overview(
        GROUP_ID,
        reviewer_ready=True,
        ai_failure_streak=3,
    )

    assert "模式：执行｜AI：已暂停" in text
    assert "今日：删 1｜警 1｜封 1｜移 1" in text
    assert "近 7 天：删 1｜警 2｜封 2｜移 2" in text
    assert "累计：删 2｜警 2｜封 3｜移 3" in text
    assert "历史清理：今日 1｜近 7 天 2｜累计 2" in text
    assert "未清警告：2 人｜3 次" in text
    assert "调用 4｜广告 2｜失败率 25.0%｜P95 400 ms" in text
    assert "队列：临时 2（到期 1）｜日志 2（到期 2｜重试 1）" in text
    assert (
        "运行告警：AI 连续失败 3 次；临时消息到期积压 1；日志重试 1；"
        "日志到期积压 2；近 7 天成员解封失败 1"
    ) in text
    assert "MTProto：身份与目标群已验证" in text
    session.probe.assert_awaited_once_with(GROUP_ID)


async def test_ai_detail_uses_persisted_pause_state_and_seven_day_metrics(
    tmp_path: Path,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nexa_guardian.status_service.datetime", FixedDateTime)
    await _insert_ai_metrics(storage)
    await storage.set_runtime_setting(GROUP_ID, "ai_paused", "1")
    service, _, _ = status_service(tmp_path, storage)

    text = await service.ai_detail(
        GROUP_ID,
        reviewer_ready=True,
        ai_failure_streak=2,
    )

    assert "实际状态：已暂停" in text
    assert "超时：12s" in text
    assert "连续失败：2" in text
    assert "近 7 天：调用 4｜广告 2｜失败率 25.0%｜P95 400 ms" in text
    assert "累计：调用 5｜广告 3｜失败 2" in text


async def test_mtproto_status_distinguishes_configuration_verification_and_busy(
    tmp_path: Path,
    storage: Storage,
) -> None:
    unconfigured, _, unconfigured_session = status_service(
        tmp_path,
        storage,
        mtproto_ready=False,
    )
    assert await unconfigured._mtproto_status(GROUP_ID) == "未配置凭据"
    unconfigured_session.probe.assert_not_awaited()

    configured, _, configured_session = status_service(tmp_path, storage)
    configured_session.probe.return_value = SessionHealth(True, False, "")
    assert await configured._mtproto_status(GROUP_ID) == "仅配置凭据，身份未验证"

    verified, _, verified_session = status_service(tmp_path, storage)
    verified_session.probe.return_value = SessionHealth(True, True, "身份与目标群已验证")
    assert await verified._mtproto_status(GROUP_ID) == "身份与目标群已验证"

    busy, _, busy_session = status_service(tmp_path, storage)
    busy_session.probe.return_value = SessionHealth(True, False, "任务运行中")
    assert await busy._mtproto_status(GROUP_ID) == "任务运行中"


async def test_overview_cancels_mtproto_probe_when_an_earlier_dependency_fails(
    tmp_path: Path,
    storage: Storage,
) -> None:
    service, bot, session = status_service(tmp_path, storage)
    probe_started = asyncio.Event()
    probe_cancelled = asyncio.Event()

    async def blocking_probe(_chat_id: int) -> SessionHealth:
        probe_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            probe_cancelled.set()

    bot.get_chat_member.side_effect = RuntimeError("Telegram unavailable")
    session.probe.side_effect = blocking_probe

    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        await service.overview(GROUP_ID, reviewer_ready=True, ai_failure_streak=0)

    assert probe_started.is_set()
    assert probe_cancelled.is_set()


async def test_ai_word_pages_are_horizontal_segmented_and_include_ids(
    tmp_path: Path,
    storage: Storage,
) -> None:
    signatures = []
    for index, display in enumerate(("免费领取 USDT", "guaranteed profit", "<推广&合作>"), 1):
        signatures.append(
            await storage.upsert_ai_signature(
                chat_id=GROUP_ID,
                normalized_signature=f"signature-{index}",
                display_signature=display,
                category="promotion",
                status="active",
                confidence=0.99,
            )
        )
    service, _, _ = status_service(tmp_path, storage)

    pages = await service.ai_word_pages(GROUP_ID, max_length=100)

    assert len(pages) == 2
    assert "　" in pages[0]
    joined = "\n".join(pages)
    assert "AI Active 特征（3） 1/2" in pages[0]
    assert "AI Active 特征（3） 2/2" in pages[1]
    assert all(f"<code>{signature.id}</code>" in joined for signature in signatures)
    assert "&lt;推广&amp;合作&gt;" in joined


async def test_pause_and_disable_operations_persist_state(
    tmp_path: Path,
    storage: Storage,
) -> None:
    signature = await storage.upsert_ai_signature(
        chat_id=GROUP_ID,
        normalized_signature="guaranteed profit",
        display_signature="guaranteed profit",
        category="promotion",
        status="active",
        confidence=0.99,
    )
    service, _, _ = status_service(tmp_path, storage)

    assert await service.set_ai_paused(GROUP_ID, True)
    assert await storage.runtime_setting(GROUP_ID, "ai_paused") == "1"
    assert not await service.set_ai_paused(GROUP_ID, True)
    assert await service.set_ai_paused(GROUP_ID, False)
    assert await storage.runtime_setting(GROUP_ID, "ai_paused") == "0"

    assert await service.disable_ai_signature(GROUP_ID, signature.id)
    assert not await service.disable_ai_signature(GROUP_ID, signature.id)
    assert await storage.ai_signatures(GROUP_ID, statuses=("active",)) == ()
