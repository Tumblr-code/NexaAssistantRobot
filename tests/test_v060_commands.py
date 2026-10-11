import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import Chat, Message, User

from nexa_guardian import moderator as moderator_module
from nexa_guardian.admin import AdminRights
from nexa_guardian.config import Settings
from nexa_guardian.history_purge import HistoryPurgeResult
from nexa_guardian.member_activity import MemberActivityTracker
from nexa_guardian.moderator import MemberActivityMiddleware, ModerationController
from nexa_guardian.storage import Storage

UTC = timezone.utc
GROUP_ID = -100
ADMIN_ID = 7
MEMBER_ID = 42


def settings(tmp_path: Path) -> Settings:
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
    )


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


def command(text: str, *, private: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(
            id=ADMIN_ID if private else GROUP_ID,
            type=ChatType.PRIVATE if private else ChatType.SUPERGROUP,
        ),
        from_user=SimpleNamespace(id=ADMIN_ID),
        sender_chat=None,
        reply_to_message=None,
        text=text,
        date=datetime.now(UTC),
        answer=AsyncMock(),
    )


def response_texts(message: SimpleNamespace) -> tuple[str, ...]:
    return tuple(str(call.args[0]) for call in message.answer.await_args_list)


def controller(
    tmp_path: Path,
    storage: Storage,
) -> tuple[ModerationController, AsyncMock]:
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.CREATOR,
        user=SimpleNamespace(id=ADMIN_ID, is_bot=False),
    )
    instance = ModerationController(
        bot,
        settings(tmp_path),
        storage,
        ai_reviewer=AsyncMock(),
    )
    instance.admins.rights = AsyncMock(return_value=AdminRights(True, True, True))
    return instance, bot


async def moderation_rows(storage: Storage) -> tuple[tuple[Any, ...], ...]:
    cursor = await storage.db.execute(
        """
        SELECT action, actor_id, target_id, reason
        FROM moderation_log
        ORDER BY id
        """
    )
    return tuple(tuple(row) for row in await cursor.fetchall())


async def test_status_ai_is_rejected_in_group(
    tmp_path: Path,
    storage: Storage,
) -> None:
    instance, _ = controller(tmp_path, storage)
    message = command("/status ai", private=False)

    await instance.status(message)  # type: ignore[arg-type]

    assert response_texts(message) == ("AI 管理仅允许授权管理员在机器人私聊中使用。",)
    assert await storage.runtime_setting(GROUP_ID, "ai_paused") is None
    assert await moderation_rows(storage) == ()


async def test_status_ai_private_controls_persist_state_words_and_actions(
    tmp_path: Path,
    storage: Storage,
) -> None:
    instance, _ = controller(tmp_path, storage)
    signature = await storage.upsert_ai_signature(
        chat_id=GROUP_ID,
        normalized_signature="claim airdrop",
        display_signature="claim airdrop",
        category="promotion",
        status="active",
        confidence=0.99,
    )

    pause = command("/status ai pause")
    await instance.status(pause)  # type: ignore[arg-type]
    assert await storage.runtime_setting(GROUP_ID, "ai_paused") == "1"
    assert "AI 审核已暂停" in response_texts(pause)[0]

    detail = command("/status ai")
    await instance.status(detail)  # type: ignore[arg-type]
    assert "实际状态：已暂停" in response_texts(detail)[0]

    words = command("/status ai words")
    await instance.status(words)  # type: ignore[arg-type]
    assert f"<code>{signature.id}</code>" in response_texts(words)[0]
    assert "claim airdrop" in response_texts(words)[0]

    delete = command(f"/status ai del {signature.id}")
    await instance.status(delete)  # type: ignore[arg-type]
    assert response_texts(delete) == (f"已停用 AI 特征 <code>{signature.id}</code>。",)
    assert await storage.active_ai_signatures(GROUP_ID) == set()

    resume = command("/status ai resume")
    await instance.status(resume)  # type: ignore[arg-type]
    assert await storage.runtime_setting(GROUP_ID, "ai_paused") == "0"
    assert response_texts(resume) == ("AI 审核已恢复。",)

    assert await moderation_rows(storage) == (
        ("ai_pause", ADMIN_ID, None, ""),
        ("ai_signature_disabled", ADMIN_ID, signature.id, ""),
        ("ai_resume", ADMIN_ID, None, ""),
    )
    all_responses = (*response_texts(pause), *response_texts(detail), *response_texts(words))
    all_responses += (*response_texts(delete), *response_texts(resume))
    assert all("原因" not in text for text in all_responses)


async def test_status_ai_pause_and_resume_are_serialized(
    tmp_path: Path,
    storage: Storage,
) -> None:
    instance, _ = controller(tmp_path, storage)
    entered_pause = asyncio.Event()
    release_pause = asyncio.Event()
    original = instance.status_service.set_ai_paused

    async def controlled(chat_id: int, paused: bool) -> bool:
        if paused:
            entered_pause.set()
            await release_pause.wait()
        return await original(chat_id, paused)

    instance.status_service.set_ai_paused = controlled  # type: ignore[method-assign]
    pause = command("/status ai pause")
    resume = command("/status ai resume")
    pause_task = asyncio.create_task(instance.status(pause))  # type: ignore[arg-type]
    await entered_pause.wait()
    resume_task = asyncio.create_task(instance.status(resume))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    release_pause.set()
    await asyncio.gather(pause_task, resume_task)

    assert await storage.runtime_setting(GROUP_ID, "ai_paused") == "0"
    assert await moderation_rows(storage) == (
        ("ai_pause", ADMIN_ID, None, ""),
        ("ai_resume", ADMIN_ID, None, ""),
    )


async def test_del_all_rejects_active_raffle_without_calling_purger(
    tmp_path: Path,
    storage: Storage,
) -> None:
    instance, _ = controller(tmp_path, storage)
    raffle = await storage.create_raffle(
        chat_id=GROUP_ID,
        creator_id=ADMIN_ID,
        keyword="参与",
        prize="测试奖品",
        winner_count=1,
        ends_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    assert await storage.set_raffle_message(raffle.id, 500)
    assert await storage.activate_raffle(raffle.id)
    instance.history_purger.purge = AsyncMock()
    message = command("/del all")

    await instance.delete_replied(message)  # type: ignore[arg-type]

    assert response_texts(message) == (
        "当前有进行中的抽奖，请先执行 <code>/lottery cancel</code>。",
    )
    instance.history_purger.purge.assert_not_called()
    assert await moderation_rows(storage) == ()


async def test_del_all_success_records_one_history_purge_without_delete(
    tmp_path: Path,
    storage: Storage,
) -> None:
    instance, _ = controller(tmp_path, storage)
    instance.history_purger.purge = AsyncMock(
        return_value=HistoryPurgeResult(deleted=12, skipped=2, failed=1)
    )
    message = command("/del all")

    await instance.delete_replied(message)  # type: ignore[arg-type]

    instance.history_purger.purge.assert_awaited_once_with(GROUP_ID)
    assert response_texts(message) == (
        "正在清理群历史，期间群聊保持开放。",
        "群历史清理完成：删除 12，跳过 2，失败 1。",
    )
    stats = await storage.chat_stats(GROUP_ID)
    assert stats.history_purges == 1
    assert stats.deleted == 0
    assert await moderation_rows(storage) == (("history_purge", ADMIN_ID, None, ""),)
    assert all("原因" not in text for text in response_texts(message))


async def test_del_all_stops_at_fifteen_minute_timeout_without_action(
    tmp_path: Path,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _ = controller(tmp_path, storage)
    instance.history_purger.purge = AsyncMock(
        return_value=HistoryPurgeResult(deleted=0, skipped=0, failed=0)
    )
    observed_timeouts: list[float | None] = []

    async def timeout_wait_for(awaitable: Any, *, timeout: float | None = None) -> Any:
        observed_timeouts.append(timeout)
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(moderator_module.asyncio, "wait_for", timeout_wait_for)
    message = command("/del all")

    await instance.delete_replied(message)  # type: ignore[arg-type]

    assert observed_timeouts == [15 * 60]
    instance.history_purger.purge.assert_called_once_with(GROUP_ID)
    assert response_texts(message) == (
        "正在清理群历史，期间群聊保持开放。",
        "群历史清理超过 15 分钟，任务已停止。",
    )
    assert await moderation_rows(storage) == ()
    assert all("原因" not in text for text in response_texts(message))


async def test_member_activity_middleware_notes_before_raffle_keyword_delete() -> None:
    now = datetime.now(UTC)
    tracker = MemberActivityTracker()
    activity = tracker.begin(
        actor_id=ADMIN_ID,
        chat_id=GROUP_ID,
        cutoff=now - timedelta(days=30),
        created_at=now - timedelta(seconds=1),
    )
    middleware = MemberActivityMiddleware(
        tracker,
        allowed_chat_ids=frozenset({GROUP_ID}),
    )
    delete_message = AsyncMock()
    event = Message(
        message_id=501,
        date=now,
        chat=Chat(id=GROUP_ID, type=ChatType.SUPERGROUP, title="Example"),
        from_user=User(id=MEMBER_ID, is_bot=False, first_name="Member"),
        text="参与",
    )

    async def raffle_keyword_handler(message: Message, _data: dict[str, Any]) -> str:
        assert tracker.active_ids(activity) == frozenset({MEMBER_ID})
        await delete_message(message.chat.id, message.message_id)
        return "joined"

    result = await middleware(raffle_keyword_handler, event, {})

    assert result == "joined"
    delete_message.assert_awaited_once_with(GROUP_ID, 501)
    assert tracker.active_ids(activity) == frozenset({MEMBER_ID})
