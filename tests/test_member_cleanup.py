import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import BanChatMember, UnbanChatMember
from aiogram.types import Chat, Message, User

from nexa_guardian.admin import AdminRights
from nexa_guardian.config import Settings
from nexa_guardian.moderator import (
    ModerationController,
    PendingInactiveCleanup,
    telegram_call_with_bounded_retry,
)
from nexa_guardian.storage import Storage

UTC = timezone.utc
ADMIN_ID = 7
GROUP_ID = -100


class MemberCleanupBot:
    def __init__(self) -> None:
        self.members: dict[int, tuple[ChatMemberStatus, bool]] = {}
        self.member_checks: list[tuple[int, int]] = []
        self.banned: list[tuple[int, int, bool]] = []
        self.ban_until_dates: list[datetime | None] = []
        self.unbanned: list[tuple[int, int, bool]] = []

    async def get_chat_administrators(self, chat_id: int, **_kwargs: object):
        assert chat_id == GROUP_ID
        return [
            SimpleNamespace(
                status=ChatMemberStatus.CREATOR,
                user=SimpleNamespace(id=ADMIN_ID, is_bot=False),
            )
        ]

    async def get_chat_member(self, chat_id: int, user_id: int):
        self.member_checks.append((chat_id, user_id))
        if user_id == ADMIN_ID:
            status, is_bot = ChatMemberStatus.CREATOR, False
        else:
            status, is_bot = self.members.get(user_id, (ChatMemberStatus.MEMBER, False))
        return SimpleNamespace(
            status=status,
            user=SimpleNamespace(id=user_id, is_bot=is_bot),
        )

    async def ban_chat_member(
        self,
        chat_id: int,
        user_id: int,
        *,
        until_date: datetime | None = None,
        revoke_messages: bool,
    ) -> bool:
        self.banned.append((chat_id, user_id, revoke_messages))
        self.ban_until_dates.append(until_date)
        return True

    async def unban_chat_member(
        self,
        chat_id: int,
        user_id: int,
        *,
        only_if_banned: bool,
    ) -> bool:
        self.unbanned.append((chat_id, user_id, only_if_banned))
        return True


def cleanup_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "bot_token": "123:ABC",
        "allowed_chat_ids": frozenset({GROUP_ID}),
        "authorized_admin_ids": frozenset({ADMIN_ID}),
        "log_chat_id": None,
        "database_path": tmp_path / "guardian.db",
        "allowed_domains": (),
        "blocked_words": (),
        "block_external_links": True,
        "block_forwards": False,
        "block_channel_mentions": False,
        "warn_limit": 3,
        "default_mute_seconds": 3600,
        "notice_ttl_seconds": 0,
        "flood_window_seconds": 8,
        "flood_max_messages": 6,
        "duplicate_window_seconds": 30,
        "duplicate_max_messages": 2,
        "dry_run": False,
        "log_level": "INFO",
    }
    values.update(overrides)
    return Settings(**values)


def private_command(text: str, *, user_id: int = ADMIN_ID) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=user_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=user_id),
        sender_chat=None,
        text=text,
        date=datetime.now(UTC),
        answer=AsyncMock(),
    )


def group_command(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=GROUP_ID, type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=ADMIN_ID),
        sender_chat=None,
        text=text,
        date=datetime.now(UTC),
        answer=AsyncMock(),
    )


@pytest.fixture
async def cleanup_storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


async def test_inactive_members_is_private_only_and_uses_observed_group_activity(
    tmp_path: Path,
    cleanup_storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(  # type: ignore[arg-type]
        bot,
        cleanup_settings(tmp_path),
        cleanup_storage,
    )
    await cleanup_storage.record_member_status(
        GROUP_ID,
        101,
        "member",
        observed_at="2000-01-01T00:00:00+00:00",
    )
    await cleanup_storage.record_member_message(
        GROUP_ID,
        202,
        observed_at="2099-01-01T00:00:00+00:00",
    )

    private = private_command("/inactive")
    group = group_command("/inactive")
    await controller.inactive_members(private)  # type: ignore[arg-type]
    await controller.inactive_members(group)  # type: ignore[arg-type]

    output = private.answer.await_args.args[0]
    assert "30" in output
    assert "101" in output
    assert "202" not in output
    assert "群内活动不代表 Telegram 最后上线" in output
    assert "私聊" in group.answer.await_args.args[0]


@pytest.mark.parametrize("days", (29, 366))
async def test_inactive_members_rejects_days_outside_30_to_365(
    tmp_path: Path,
    cleanup_storage: Storage,
    days: int,
) -> None:
    controller = ModerationController(  # type: ignore[arg-type]
        MemberCleanupBot(),
        cleanup_settings(tmp_path),
        cleanup_storage,
    )
    message = private_command(f"/inactive {days}")

    await controller.inactive_members(message)  # type: ignore[arg-type]

    assert "30" in message.answer.await_args.args[0]
    assert "365" in message.answer.await_args.args[0]


async def test_inactive_cleanup_requires_the_sole_private_admin_and_restrict_right(
    tmp_path: Path,
    cleanup_storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(  # type: ignore[arg-type]
        bot,
        cleanup_settings(tmp_path),
        cleanup_storage,
    )

    unauthorized = private_command("/cleanupinactive 30", user_id=99)
    group = group_command("/cleanupinactive 30")
    await controller.cleanup_inactive(unauthorized)  # type: ignore[arg-type]
    await controller.cleanup_inactive(group)  # type: ignore[arg-type]

    assert "授权管理员" in unauthorized.answer.await_args.args[0]
    assert "私聊" in group.answer.await_args.args[0]
    assert bot.banned == []

    controller.admins.rights = AsyncMock(
        return_value=AdminRights(
            is_admin=True,
            can_delete_messages=True,
            can_restrict_members=False,
        )
    )
    no_right = private_command("/cleanupinactive 30")
    await controller.cleanup_inactive(no_right)  # type: ignore[arg-type]

    assert "封禁/限制成员" in no_right.answer.await_args.args[0]
    assert bot.banned == []


async def test_inactive_cleanup_requires_a_valid_unexpired_confirmation_code(
    tmp_path: Path,
    cleanup_storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr("nexa_guardian.moderator.time.monotonic", lambda: clock["now"])
    bot = MemberCleanupBot()
    controller = ModerationController(  # type: ignore[arg-type]
        bot,
        cleanup_settings(tmp_path),
        cleanup_storage,
    )
    await cleanup_storage.record_member_message(
        GROUP_ID,
        101,
        observed_at="2000-01-01T00:00:00+00:00",
    )

    preview = private_command("/cleanupinactive 30")
    await controller.cleanup_inactive(preview)  # type: ignore[arg-type]

    preview_text = preview.answer.await_args.args[0]
    match = re.search(r"/cleanupinactive\s+([A-Z0-9]{6})\s+confirm", preview_text)
    assert match is not None
    code = match.group(1)
    assert bot.banned == []
    assert "群内活动不代表 Telegram 最后上线" in preview_text

    wrong = private_command("/cleanupinactive WRONG1 confirm")
    await controller.cleanup_inactive(wrong)  # type: ignore[arg-type]
    assert "无效" in wrong.answer.await_args.args[0]
    assert bot.banned == []

    clock["now"] += 601
    expired = private_command(f"/cleanupinactive {code} confirm")
    await controller.cleanup_inactive(expired)  # type: ignore[arg-type]
    assert "过期" in expired.answer.await_args.args[0]
    assert bot.banned == []


async def test_confirmed_inactive_cleanup_rechecks_every_candidate_and_only_kicks_safe_targets(
    tmp_path: Path,
    cleanup_storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(  # type: ignore[arg-type]
        bot,
        cleanup_settings(tmp_path),
        cleanup_storage,
    )
    candidates = (101, 102, 103, 104, 105, 106)
    for user_id in candidates:
        await cleanup_storage.record_member_message(
            GROUP_ID,
            user_id,
            observed_at="2000-01-01T00:00:00+00:00",
        )

    preview = private_command("/cleanupinactive 30")
    await controller.cleanup_inactive(preview)  # type: ignore[arg-type]
    match = re.search(
        r"/cleanupinactive\s+([A-Z0-9]{6})\s+confirm",
        preview.answer.await_args.args[0],
    )
    assert match is not None
    code = match.group(1)
    assert bot.banned == []

    bot.members.update(
        {
            102: (ChatMemberStatus.ADMINISTRATOR, False),
            103: (ChatMemberStatus.MEMBER, True),
            104: (ChatMemberStatus.MEMBER, False),
            105: (ChatMemberStatus.LEFT, False),
            106: (ChatMemberStatus.MEMBER, False),
        }
    )
    await cleanup_storage.add_trusted_user(GROUP_ID, 104, ADMIN_ID)
    await cleanup_storage.record_member_message(
        GROUP_ID,
        106,
        observed_at="2099-01-01T00:00:00+00:00",
    )
    bot.member_checks.clear()

    confirm = private_command(f"/cleanupinactive {code} confirm")
    await controller.cleanup_inactive(confirm)  # type: ignore[arg-type]

    candidate_checks = {
        (chat_id, user_id) for chat_id, user_id in bot.member_checks if user_id != ADMIN_ID
    }
    assert candidate_checks == {(GROUP_ID, user_id) for user_id in candidates}
    assert bot.banned == [(GROUP_ID, 101, False)]
    assert bot.ban_until_dates[0] is not None
    assert bot.ban_until_dates[0] > datetime.now(UTC)
    assert bot.unbanned == [(GROUP_ID, 101, True)]
    rows = await cleanup_storage.db.execute_fetchall(
        "SELECT action, actor_id, target_id FROM moderation_log WHERE action = 'cleanup_inactive'"
    )
    assert [tuple(row) for row in rows] == [("cleanup_inactive", ADMIN_ID, 101)]
    summary = confirm.answer.await_args.args[0]
    assert "已移出 1" in summary
    assert "跳过 5" in summary


async def test_cleanup_stops_after_unban_failure_and_uses_temporary_ban_fallback(
    tmp_path: Path,
    cleanup_storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(  # type: ignore[arg-type]
        bot,
        cleanup_settings(tmp_path),
        cleanup_storage,
    )
    for user_id in (101, 102):
        await cleanup_storage.record_member_message(
            GROUP_ID,
            user_id,
            observed_at="2000-01-01T00:00:00+00:00",
        )
    preview = private_command("/cleanupinactive 30")
    await controller.cleanup_inactive(preview)  # type: ignore[arg-type]
    code = re.search(
        r"/cleanupinactive\s+([A-Z0-9]{6})\s+confirm",
        preview.answer.await_args.args[0],
    )
    assert code is not None
    bot.unban_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(
            method=UnbanChatMember(chat_id=GROUP_ID, user_id=101),
            message="temporary failure",
        )
    )

    confirm = private_command(f"/cleanupinactive {code.group(1)} confirm")
    await controller.cleanup_inactive(confirm)  # type: ignore[arg-type]

    assert bot.banned == [(GROUP_ID, 101, False)]
    assert bot.ban_until_dates[0] is not None
    assert bot.ban_until_dates[0] > datetime.now(UTC)
    assert "解除封禁失败" in confirm.answer.await_args.args[0]
    assert "未继续处理 1" in confirm.answer.await_args.args[0]


async def test_cleanup_and_permanent_ban_are_serialized_per_member(
    tmp_path: Path,
    cleanup_storage: Storage,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[str, bool]] = []

    class BlockingBot(MemberCleanupBot):
        async def ban_chat_member(
            self,
            chat_id: int,
            user_id: int,
            *,
            until_date: datetime | None = None,
            revoke_messages: bool,
        ) -> bool:
            calls.append(("ban", revoke_messages))
            if not revoke_messages:
                started.set()
                await release.wait()
            return await super().ban_chat_member(
                chat_id,
                user_id,
                until_date=until_date,
                revoke_messages=revoke_messages,
            )

        async def unban_chat_member(
            self,
            chat_id: int,
            user_id: int,
            *,
            only_if_banned: bool,
        ) -> bool:
            calls.append(("unban", only_if_banned))
            return await super().unban_chat_member(
                chat_id,
                user_id,
                only_if_banned=only_if_banned,
            )

    bot = BlockingBot()
    controller = ModerationController(  # type: ignore[arg-type]
        bot,
        cleanup_settings(tmp_path),
        cleanup_storage,
    )
    await cleanup_storage.record_member_message(
        GROUP_ID,
        101,
        observed_at="2000-01-01T00:00:00+00:00",
    )
    activity = await cleanup_storage.member_activity(GROUP_ID, 101)
    assert activity is not None
    plan = PendingInactiveCleanup(
        actor_id=ADMIN_ID,
        chat_id=GROUP_ID,
        days=30,
        cutoff="2020-01-01T00:00:00+00:00",
        members=((101, controller._member_activity_stamp(activity)),),
        expires_at=float("inf"),
    )
    cleanup = asyncio.create_task(
        controller._execute_inactive_cleanup(private_command("cleanup"), plan)  # type: ignore[arg-type]
    )
    await started.wait()

    async def permanent_ban() -> None:
        async with controller._member_action_lock(GROUP_ID, 101):
            await bot.ban_chat_member(GROUP_ID, 101, revoke_messages=True)

    permanent = asyncio.create_task(permanent_ban())
    await asyncio.sleep(0)
    assert calls == [("ban", False)]
    release.set()
    await asyncio.gather(cleanup, permanent)

    assert calls == [("ban", False), ("unban", True), ("ban", True)]


async def test_new_member_message_wins_lock_before_cleanup_recheck(
    tmp_path: Path,
    cleanup_storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(  # type: ignore[arg-type]
        bot,
        cleanup_settings(tmp_path),
        cleanup_storage,
    )
    await cleanup_storage.record_member_message(
        GROUP_ID,
        101,
        observed_at="2000-01-01T00:00:00+00:00",
    )
    activity = await cleanup_storage.member_activity(GROUP_ID, 101)
    assert activity is not None
    plan = PendingInactiveCleanup(
        actor_id=ADMIN_ID,
        chat_id=GROUP_ID,
        days=30,
        cutoff="2020-01-01T00:00:00+00:00",
        members=((101, controller._member_activity_stamp(activity)),),
        expires_at=float("inf"),
    )
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=GROUP_ID, type=ChatType.SUPERGROUP, title="Test"),
        from_user=User(id=101, is_bot=False, first_name="Member"),
        text="normal chat",
    )
    lock = controller._member_action_lock(GROUP_ID, 101)
    await lock.acquire()
    activity_task = asyncio.create_task(controller.moderate_message(message))
    await asyncio.sleep(0)
    cleanup_task = asyncio.create_task(
        controller._execute_inactive_cleanup(private_command("cleanup"), plan)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    lock.release()
    await asyncio.gather(activity_task, cleanup_task)

    assert bot.banned == []


async def test_telegram_retry_after_is_bounded_and_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = AsyncMock(
        side_effect=[
            TelegramRetryAfter(
                method=BanChatMember(chat_id=GROUP_ID, user_id=101),
                message="retry",
                retry_after=1,
            ),
            "ok",
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("nexa_guardian.moderator.asyncio.sleep", sleep)

    assert await telegram_call_with_bounded_retry(operation) == "ok"
    sleep.assert_awaited_once_with(1.1)
    assert operation.await_count == 2
