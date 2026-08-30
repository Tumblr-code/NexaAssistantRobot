import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import BanChatMember, UnbanChatMember

from nexa_guardian.admin import AdminRights
from nexa_guardian.config import Settings
from nexa_guardian.member_cleanup import (
    REASON_DELETED,
    REASON_LAST_MONTH,
    REASON_SILENT,
    MemberCandidate,
    MemberScanError,
    MemberScanResult,
    MemberState,
)
from nexa_guardian.moderator import (
    ModerationController,
    PendingMemberCleanup,
    telegram_call_with_bounded_retry,
)
from nexa_guardian.storage import Storage

ADMIN_ID = 7
GROUP_ID = -100
UTC = timezone.utc


class MemberCleanupBot:
    def __init__(self) -> None:
        self.members: dict[int, tuple[ChatMemberStatus, bool]] = {}
        self.banned: list[tuple[int, int, bool]] = []
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
        if user_id == ADMIN_ID:
            status, is_bot = ChatMemberStatus.CREATOR, False
        else:
            status, is_bot = self.members.get(user_id, (ChatMemberStatus.MEMBER, False))
        return SimpleNamespace(status=status, user=SimpleNamespace(id=user_id, is_bot=is_bot))

    async def ban_chat_member(
        self,
        chat_id: int,
        user_id: int,
        *,
        until_date: datetime | None = None,
        revoke_messages: bool,
    ) -> bool:
        if not revoke_messages:
            assert until_date is not None and until_date > datetime.now(UTC)
        self.banned.append((chat_id, user_id, revoke_messages))
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


class FakeScanner:
    def __init__(self, candidates: tuple[MemberCandidate, ...]) -> None:
        self.candidates = candidates
        self.history_complete = True
        self.scan_error: Exception | None = None
        self.active_since_preview: frozenset[int] = frozenset()
        self.activity_during_preview_at: datetime | None = None
        self.current = {
            candidate.user_id: MemberState(
                user_id=candidate.user_id,
                deleted=REASON_DELETED in candidate.reasons,
                bot=False,
                administrator=False,
                joined_at=candidate.joined_at,
                status_reason=(
                    REASON_LAST_MONTH if REASON_LAST_MONTH in candidate.reasons else None
                ),
                last_online_at=candidate.last_online_at,
            )
            for candidate in candidates
        }

    async def scan(self, chat_id: int, *, cutoff: datetime) -> MemberScanResult:
        assert chat_id == GROUP_ID
        assert cutoff < datetime.now(UTC)
        if self.scan_error is not None:
            raise self.scan_error
        return MemberScanResult(
            candidates=self.candidates,
            history_complete=self.history_complete,
            participants_scanned=100,
        )

    async def current_members(
        self,
        chat_id: int,
        *,
        cutoff: datetime,
        activity_since: datetime,
        user_ids: frozenset[int],
    ) -> tuple[dict[int, MemberState], frozenset[int]]:
        assert chat_id == GROUP_ID
        assert activity_since > cutoff
        active_ids = self.active_since_preview.intersection(user_ids)
        if (
            self.activity_during_preview_at is not None
            and activity_since <= self.activity_during_preview_at
        ):
            active_ids = active_ids.union(user_ids)
        return (
            {user_id: self.current[user_id] for user_id in user_ids if user_id in self.current},
            active_ids,
        )


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
        default_mute_seconds=3600,
        notice_ttl_seconds=0,
        flood_window_seconds=8,
        flood_max_messages=6,
        duplicate_window_seconds=30,
        duplicate_max_messages=2,
        dry_run=False,
        log_level="INFO",
    )


def command(text: str, *, private: bool = True, user_id: int = ADMIN_ID) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(
            id=user_id if private else GROUP_ID,
            type=ChatType.PRIVATE if private else ChatType.SUPERGROUP,
        ),
        from_user=SimpleNamespace(id=user_id),
        sender_chat=None,
        text=text,
        date=datetime.now(UTC),
        answer=AsyncMock(),
    )


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


def candidate(user_id: int, *reasons: str) -> MemberCandidate:
    return MemberCandidate(
        user_id=user_id,
        reasons=tuple(reasons),
        last_online_at=None,
        joined_at=datetime.now(UTC) - timedelta(days=90),
    )


async def test_del_members_is_private_only_and_requires_restrict_right(
    tmp_path: Path,
    storage: Storage,
) -> None:
    controller = ModerationController(MemberCleanupBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    controller.member_scanner = FakeScanner((candidate(101, REASON_DELETED),))  # type: ignore[assignment]

    group = command("/del members", private=False)
    await controller.delete_replied(group)  # type: ignore[arg-type]
    assert "私聊" in group.answer.await_args.args[0]

    controller.admins.rights = AsyncMock(return_value=AdminRights(True, True, False))
    denied = command("/del members")
    await controller.delete_replied(denied)  # type: ignore[arg-type]
    assert "封禁/限制成员" in denied.answer.await_args.args[0]


async def test_del_members_preview_creates_confirmation_and_reports_degraded_history(
    tmp_path: Path,
    storage: Storage,
) -> None:
    controller = ModerationController(MemberCleanupBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = FakeScanner(
        (
            candidate(101, REASON_DELETED),
            candidate(102, REASON_LAST_MONTH, REASON_SILENT),
        )
    )
    scanner.history_complete = False
    controller.member_scanner = scanner  # type: ignore[assignment]
    preview = command("/del members")

    await controller.delete_replied(preview)  # type: ignore[arg-type]

    text = preview.answer.await_args.args[0]
    assert "101" in text and "已注销" in text
    assert "102" in text and "上月上线（模糊）" in text
    assert "未使用“30 天无发言”条件" in text
    assert re.search(r"/del members [A-F0-9]{6} confirm", text)


async def test_del_members_scan_failure_does_not_create_plan(
    tmp_path: Path,
    storage: Storage,
) -> None:
    controller = ModerationController(MemberCleanupBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = FakeScanner(())
    scanner.scan_error = MemberScanError("未配置")
    controller.member_scanner = scanner  # type: ignore[assignment]
    message = command("/del members")

    await controller.delete_replied(message)  # type: ignore[arg-type]

    assert "成员扫描失败" in message.answer.await_args.args[0]
    assert controller._member_cleanup_plans == {}


async def test_del_members_limits_plan_and_rejects_expired_confirmation(
    tmp_path: Path,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr("nexa_guardian.moderator.time.monotonic", lambda: clock["now"])
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    controller.member_scanner = FakeScanner(
        tuple(candidate(user_id, REASON_DELETED) for user_id in range(100, 155))
    )  # type: ignore[assignment]
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    text = preview.answer.await_args.args[0]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", text)
    assert code is not None
    assert "（50/55）" in text
    assert len(next(iter(controller._member_cleanup_plans.values())).members) == 50

    wrong = command("/del members BAD999 confirm")
    await controller.delete_replied(wrong)  # type: ignore[arg-type]
    assert "无效或已过期" in wrong.answer.await_args.args[0]

    clock["now"] = 701.0
    expired = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(expired)  # type: ignore[arg-type]
    assert "无效或已过期" in expired.answer.await_args.args[0]
    assert bot.banned == []


async def test_del_members_confirmation_rechecks_and_kicks_only_safe_targets(
    tmp_path: Path,
    storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = FakeScanner(
        tuple(candidate(user_id, REASON_DELETED) for user_id in (101, 102, 103, 104, 105))
    )
    controller.member_scanner = scanner  # type: ignore[assignment]
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", preview.answer.await_args.args[0])
    assert code is not None

    scanner.current[102] = MemberState(102, True, True, False, None, None, None)
    scanner.current[103] = MemberState(103, True, False, True, None, None, None)
    bot.members[104] = (ChatMemberStatus.LEFT, False)
    await storage.add_trusted_user(GROUP_ID, 105, ADMIN_ID)
    confirm = command(f"/del members {code.group(1)} confirm")

    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == [(GROUP_ID, 101, False)]
    assert bot.unbanned == [(GROUP_ID, 101, True)]
    assert "已移出 1" in confirm.answer.await_args.args[0]
    rows = await storage.db.execute_fetchall(
        "SELECT action, actor_id, target_id, reason FROM moderation_log "
        "WHERE action = 'cleanup_inactive'"
    )
    assert [(row[0], row[1], row[2]) for row in rows] == [("cleanup_inactive", ADMIN_ID, 101)]
    assert all(not row[3] for row in rows)


async def test_del_members_skips_member_active_after_preview(
    tmp_path: Path,
    storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    controller.member_scanner = FakeScanner(  # type: ignore[assignment]
        (candidate(101, REASON_LAST_MONTH, REASON_SILENT),)
    )
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", preview.answer.await_args.args[0])
    assert code is not None
    await storage.record_member_message(GROUP_ID, 101, observed_at=datetime.now(UTC).isoformat())

    confirm = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == []
    assert "跳过 1" in confirm.answer.await_args.args[0]


async def test_del_members_uses_mtproto_activity_recheck_after_preview(
    tmp_path: Path,
    storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = FakeScanner((candidate(101, REASON_LAST_MONTH),))
    controller.member_scanner = scanner  # type: ignore[assignment]
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", preview.answer.await_args.args[0])
    assert code is not None
    scanner.active_since_preview = frozenset({101})

    confirm = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == []
    assert "跳过 1" in confirm.answer.await_args.args[0]


async def test_del_members_recheck_window_starts_before_initial_scan(
    tmp_path: Path,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    clock = {"now": started_at}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return clock["now"]

    class GapScanner(FakeScanner):
        async def scan(self, chat_id: int, *, cutoff: datetime) -> MemberScanResult:
            result = await super().scan(chat_id, cutoff=cutoff)
            self.activity_during_preview_at = started_at + timedelta(seconds=1)
            clock["now"] = started_at + timedelta(seconds=5)
            return result

    monkeypatch.setattr("nexa_guardian.moderator.datetime", FakeDateTime)
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = GapScanner((candidate(101, REASON_LAST_MONTH),))
    controller.member_scanner = scanner  # type: ignore[assignment]
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    plan = next(iter(controller._member_cleanup_plans.values()))
    assert plan.created_at == started_at
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", preview.answer.await_args.args[0])
    assert code is not None

    confirm = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == []
    assert "跳过 1" in confirm.answer.await_args.args[0]


async def test_del_members_stops_after_unban_failure(
    tmp_path: Path,
    storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    bot.unban_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(
            method=UnbanChatMember(chat_id=GROUP_ID, user_id=101),
            message="failed",
        )
    )
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    controller.member_scanner = FakeScanner(
        (candidate(101, REASON_DELETED), candidate(102, REASON_DELETED))
    )  # type: ignore[assignment]
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", preview.answer.await_args.args[0])
    assert code is not None

    confirm = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == [(GROUP_ID, 101, False)]
    assert "解除封禁失败" in confirm.answer.await_args.args[0]
    assert "未继续处理 1" in confirm.answer.await_args.args[0]


async def test_member_cleanup_and_permanent_ban_are_serialized(
    tmp_path: Path,
    storage: Storage,
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
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    member = candidate(101, REASON_DELETED)
    controller.member_scanner = FakeScanner((member,))  # type: ignore[assignment]
    plan = PendingMemberCleanup(
        actor_id=ADMIN_ID,
        chat_id=GROUP_ID,
        cutoff=datetime.now(UTC) - timedelta(days=30),
        created_at=datetime.now(UTC),
        members=(member,),
        expires_at=float("inf"),
    )
    cleanup = asyncio.create_task(
        controller._execute_member_cleanup(command("cleanup"), plan)  # type: ignore[arg-type]
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
