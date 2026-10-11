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
from nexa_guardian.member_activity import MemberActivityTracker
from nexa_guardian.member_cleanup import (
    REASON_DELETED,
    REASON_LAST_MONTH,
    REASON_SILENT,
    MemberCandidate,
    MemberScanError,
    MemberScanResult,
    MemberState,
)
from nexa_guardian.member_cleanup_service import (
    PendingMemberCleanup,
    telegram_call_with_bounded_retry,
)
from nexa_guardian.moderator import ModerationController
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


def response_text(message: SimpleNamespace) -> str:
    progress = message.answer.return_value
    if progress.edit_text.await_args is not None:
        return str(progress.edit_text.await_args.args[0])
    return str(message.answer.await_args.args[0])


def install_scanner(controller: ModerationController, scanner: FakeScanner) -> None:
    controller.member_scanner = scanner  # type: ignore[assignment]
    controller.member_cleanup.scanner = scanner  # type: ignore[assignment]


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


async def add_trusted(storage: Storage, user_id: int) -> None:
    await storage.db.execute(
        "INSERT INTO trusted_users VALUES (?, ?, ?, ?)",
        (GROUP_ID, user_id, ADMIN_ID, datetime.now(UTC).isoformat()),
    )
    await storage.db.commit()


async def test_del_members_is_private_only_and_requires_restrict_right(
    tmp_path: Path,
    storage: Storage,
) -> None:
    controller = ModerationController(MemberCleanupBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    install_scanner(controller, FakeScanner((candidate(101, REASON_DELETED),)))

    group = command("/del members", private=False)
    await controller.delete_replied(group)  # type: ignore[arg-type]
    assert "私聊" in response_text(group)

    controller.admins.rights = AsyncMock(return_value=AdminRights(True, True, False))
    denied = command("/del members")
    await controller.delete_replied(denied)  # type: ignore[arg-type]
    assert "封禁/限制成员" in response_text(denied)


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
    install_scanner(controller, scanner)
    preview = command("/del members")

    await controller.delete_replied(preview)  # type: ignore[arg-type]

    text = response_text(preview)
    assert "101" in text and "已注销" in text
    assert "102" in text and "Telegram 模糊状态：约一周至一个月" in text
    assert "未使用“30 天无发言”条件" in text
    assert re.search(r"/del members [A-F0-9]{6} confirm", text)


async def test_del_members_scan_failure_does_not_create_plan(
    tmp_path: Path,
    storage: Storage,
) -> None:
    controller = ModerationController(MemberCleanupBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = FakeScanner(())
    scanner.scan_error = MemberScanError("未配置")
    install_scanner(controller, scanner)
    message = command("/del members")

    await controller.delete_replied(message)  # type: ignore[arg-type]

    assert "成员扫描失败" in response_text(message)
    assert controller.member_cleanup._plans == {}


async def test_del_members_limits_plan_and_rejects_expired_confirmation(
    tmp_path: Path,
    storage: Storage,
) -> None:
    clock = {"now": 100.0}
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    tracker = MemberActivityTracker(
        default_ttl_seconds=600,
        clock=lambda: clock["now"],
    )
    controller.member_cleanup.activity = tracker
    controller.member_activity_tracker = tracker
    install_scanner(
        controller,
        FakeScanner(tuple(candidate(user_id, REASON_DELETED) for user_id in range(100, 155))),
    )
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    text = response_text(preview)
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", text)
    assert code is not None
    assert "（50/55）" in text
    assert len(next(iter(controller.member_cleanup._plans.values())).members) == 50

    wrong = command("/del members BAD999 confirm")
    await controller.delete_replied(wrong)  # type: ignore[arg-type]
    assert "无效或已过期" in response_text(wrong)

    clock["now"] = 701.0
    expired = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(expired)  # type: ignore[arg-type]
    assert "无效或已过期" in response_text(expired)
    assert bot.banned == []


async def test_del_members_skips_member_active_after_preview(
    tmp_path: Path,
    storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    install_scanner(
        controller,
        FakeScanner((candidate(101, REASON_LAST_MONTH, REASON_SILENT),)),
    )
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", response_text(preview))
    assert code is not None
    assert controller.member_cleanup.activity.note(
        GROUP_ID,
        101,
        observed_at=datetime.now(UTC),
    )

    confirm = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == []
    assert "跳过 1" in response_text(confirm)
    rows = await storage.db.execute_fetchall("SELECT * FROM member_activity")
    assert rows == []


async def test_del_members_uses_mtproto_activity_recheck_after_preview(
    tmp_path: Path,
    storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = FakeScanner((candidate(101, REASON_LAST_MONTH),))
    install_scanner(controller, scanner)
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", response_text(preview))
    assert code is not None
    scanner.active_since_preview = frozenset({101})

    confirm = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == []
    assert "跳过 1" in response_text(confirm)


async def test_del_members_skips_silent_candidate_who_rejoined_after_preview(
    tmp_path: Path,
    storage: Storage,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = FakeScanner((candidate(101, REASON_SILENT),))
    install_scanner(controller, scanner)
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", response_text(preview))
    assert code is not None
    current = scanner.current[101]
    scanner.current[101] = MemberState(
        user_id=current.user_id,
        deleted=current.deleted,
        bot=current.bot,
        administrator=current.administrator,
        joined_at=datetime.now(UTC),
        status_reason=current.status_reason,
        last_online_at=current.last_online_at,
    )

    confirm = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == []
    assert "跳过 1" in response_text(confirm)


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
            assert controller.member_cleanup.activity.note(
                GROUP_ID,
                101,
                observed_at=started_at + timedelta(seconds=1),
            )
            clock["now"] = started_at + timedelta(seconds=5)
            return result

    monkeypatch.setattr("nexa_guardian.member_cleanup_service.datetime", FakeDateTime)
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = GapScanner((candidate(101, REASON_LAST_MONTH),))
    install_scanner(controller, scanner)
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    assert controller.member_cleanup._plans == {}
    assert bot.banned == []
    assert "没有清理候选" in response_text(preview)


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
    install_scanner(
        controller,
        FakeScanner((candidate(101, REASON_DELETED), candidate(102, REASON_DELETED))),
    )
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", response_text(preview))
    assert code is not None

    confirm = command(f"/del members {code.group(1)} confirm")
    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert bot.banned == [(GROUP_ID, 101, False)]
    assert "解除封禁失败" in response_text(confirm)
    assert "未继续处理 1" in response_text(confirm)


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
    install_scanner(controller, FakeScanner((member,)))
    created_at = datetime.now(UTC)
    activity_session = controller.member_cleanup.activity.begin(
        actor_id=ADMIN_ID,
        chat_id=GROUP_ID,
        cutoff=created_at - timedelta(days=30),
        created_at=created_at,
    )
    controller.member_cleanup.activity.bind(
        activity_session,
        confirmation_code="ABC123",
        candidate_ids=(member.user_id,),
    )
    activity_plan = controller.member_cleanup.activity.start_execution(
        actor_id=ADMIN_ID,
        confirmation_code="ABC123",
    )
    assert activity_plan is not None
    plan = PendingMemberCleanup(
        actor_id=ADMIN_ID,
        chat_id=GROUP_ID,
        cutoff=created_at - timedelta(days=30),
        created_at=created_at,
        members=(member,),
        activity_plan=activity_plan,
    )
    cleanup = asyncio.create_task(
        controller.member_cleanup._execute(command("cleanup"), plan)  # type: ignore[arg-type]
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
    controller.member_cleanup.activity.finish(activity_plan)

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


async def test_del_members_confirmation_rechecks_and_kicks_only_safe_targets(
    tmp_path: Path,
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = MemberCleanupBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    scanner = FakeScanner(
        tuple(candidate(user_id, REASON_DELETED) for user_id in (101, 102, 103, 104, 105, 106))
    )
    install_scanner(controller, scanner)
    await add_trusted(storage, 105)
    trusted_lookup = AsyncMock(wraps=storage.trusted_user_ids)
    monkeypatch.setattr(storage, "trusted_user_ids", trusted_lookup)
    preview = command("/del members")
    await controller.delete_replied(preview)  # type: ignore[arg-type]
    preview_text = response_text(preview)
    code = re.search(r"/del members ([A-F0-9]{6}) confirm", preview_text)
    assert code is not None
    assert "<code>105</code>" not in preview_text
    plan = next(iter(controller.member_cleanup._plans.values()))
    assert {item.user_id for item in plan.members} == {101, 102, 103, 104, 106}

    scanner.current[103] = MemberState(103, True, False, True, None, None, None)
    await add_trusted(storage, 102)
    confirm = command(f"/del members {code.group(1)} confirm")

    await controller.delete_replied(confirm)  # type: ignore[arg-type]

    assert sorted(bot.banned) == [(GROUP_ID, user_id, False) for user_id in (101, 104, 106)]
    assert sorted(bot.unbanned) == [(GROUP_ID, user_id, True) for user_id in (101, 104, 106)]
    assert "已移出 3" in response_text(confirm)
    assert "跳过 2" in response_text(confirm)
    assert trusted_lookup.await_count == 2
    rows = await storage.db.execute_fetchall(
        "SELECT action, actor_id, target_id, reason FROM moderation_log "
        "WHERE action = 'member_cleanup'"
    )
    assert sorted((row[0], row[1], row[2]) for row in rows) == [
        ("member_cleanup", ADMIN_ID, user_id) for user_id in (101, 104, 106)
    ]
    assert all(not row[3] for row in rows)
