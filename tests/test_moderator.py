import asyncio
import gc
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import BanChatMember
from aiogram.types import Chat, Message, MessageEntity, PhotoSize, User
from aiohttp import ClientPayloadError

from nexa_guardian.ai_contract import AI_MODEL
from nexa_guardian.ai_review import AIReviewResult, AIReviewTimeout
from nexa_guardian.config import Settings
from nexa_guardian.detection import Violation
from nexa_guardian.moderator import (
    AUTO_BAN_MEMORY_MAX,
    ModerationController,
    muted_permissions,
    stats_cutoffs,
)
from nexa_guardian.storage import Storage

UTC = timezone.utc


class FakeBot:
    def __init__(
        self,
        admin_ids: set[int] | None = None,
        anonymous_admin_ids: set[int] | None = None,
    ) -> None:
        self.admin_ids = admin_ids or set()
        self.anonymous_admin_ids = anonymous_admin_ids or set()
        self.deleted: list[tuple[int, int]] = []
        self.restricted: list[tuple[int, int]] = []
        self.banned: list[tuple[int, int, bool]] = []
        self.unbanned: list[tuple[int, int]] = []
        self.sent: list[tuple[int, str]] = []

    async def get_chat_administrators(self, chat_id: int, **kwargs):
        return [
            SimpleNamespace(
                user=SimpleNamespace(id=user_id, is_bot=False),
                status=ChatMemberStatus.CREATOR,
                is_anonymous=user_id in self.anonymous_admin_ids,
            )
            for user_id in self.admin_ids | self.anonymous_admin_ids
        ]

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True

    async def restrict_chat_member(self, chat_id: int, user_id: int, *args, **kwargs) -> bool:
        self.restricted.append((chat_id, user_id))
        return True

    async def ban_chat_member(self, chat_id: int, user_id: int, *, revoke_messages: bool) -> bool:
        self.banned.append((chat_id, user_id, revoke_messages))
        return True

    async def unban_chat_member(self, chat_id: int, user_id: int, *, only_if_banned: bool) -> bool:
        self.unbanned.append((chat_id, user_id))
        return True

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id: int, user_id: int):
        status = (
            ChatMemberStatus.CREATOR
            if user_id in self.admin_ids | self.anonymous_admin_ids
            else ChatMemberStatus.MEMBER
        )
        return SimpleNamespace(status=status)


def settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "bot_token": "123:ABC",
        "allowed_chat_ids": frozenset({-100}),
        "authorized_admin_ids": frozenset(),
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


def group_message(user_id: int, text: str, *, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        from_user=User(id=user_id, is_bot=False, first_name="Member"),
        text=text,
    )


def group_photo_message(
    user_id: int,
    *,
    caption: str | None = None,
    file_size: int = 10,
    message_id: int = 1,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        from_user=User(id=user_id, is_bot=False, first_name="Member"),
        photo=[
            PhotoSize(
                file_id="photo",
                file_unique_id="photo-unique",
                width=1,
                height=1,
                file_size=file_size,
            )
        ],
        caption=caption,
    )


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


async def test_external_link_is_deleted_and_warned(tmp_path: Path, storage: Storage) -> None:
    bot = FakeBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "https://evil.example"))

    assert bot.deleted == [(-100, 1)]
    assert await storage.warning_count(-100, 42) == 1
    stats = await storage.chat_stats(-100)
    assert stats.deleted == 1
    controller._notice.assert_awaited_once()
    notice = controller._notice.await_args.args[1]
    assert "非白名单外链" not in notice
    assert "原因" not in notice
    action = await storage.db.execute_fetchall(
        "SELECT reason FROM moderation_log WHERE chat_id = ?",
        (-100,),
    )
    warning = await storage.db.execute_fetchall(
        "SELECT last_reason FROM warnings WHERE chat_id = ? AND user_id = ?",
        (-100, 42),
    )
    assert action[0]["reason"] == ""
    assert warning[0]["last_reason"] == ""


async def test_warn_limit_triggers_automatic_ban(tmp_path: Path, storage: Storage) -> None:
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, warn_limit=1),  # type: ignore[arg-type]
        storage,
    )
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "https://evil.example"))

    assert bot.banned == [(-100, 42, True)]
    assert bot.restricted == []
    assert await storage.warning_count(-100, 42) == 0
    stats = await storage.chat_stats(-100)
    assert stats.banned == 1
    assert stats.warned == 1
    notice = controller._notice.await_args.args[1]
    assert "用户已封禁" in notice
    assert "禁言" not in notice


async def test_manual_third_warning_bans_and_clears_warning(
    tmp_path: Path, storage: Storage
) -> None:
    admin_id = 7873158072
    bot = FakeBot(admin_ids={admin_id})
    controller = ModerationController(
        bot,
        settings(
            tmp_path,
            authorized_admin_ids=frozenset({admin_id}),
            warn_limit=1,
        ),  # type: ignore[arg-type]
        storage,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/warn 42",
        reply_to_message=None,
        answer=AsyncMock(),
    )

    await controller.warn(message)  # type: ignore[arg-type]

    assert bot.banned == [(-100, 42, True)]
    assert await storage.warning_count(-100, 42) == 0
    message.answer.assert_awaited_once_with("已警告 <code>42</code>；达到阈值，已封禁。")
    stats = await storage.chat_stats(-100)
    assert stats.warned == 1
    assert stats.banned == 1


async def test_admin_and_dry_run_are_not_mutated(tmp_path: Path, storage: Storage) -> None:
    admin_bot = FakeBot(admin_ids={42})
    admin_controller = ModerationController(
        admin_bot,
        settings(tmp_path),
        storage,  # type: ignore[arg-type]
    )
    await admin_controller.moderate_message(group_message(42, "https://evil.example"))
    assert admin_bot.deleted == []

    dry_bot = FakeBot()
    dry_controller = ModerationController(
        dry_bot,
        settings(tmp_path, dry_run=True),  # type: ignore[arg-type]
        storage,
    )
    await dry_controller.moderate_message(group_message(43, "https://evil.example", message_id=2))
    assert dry_bot.deleted == []
    assert await storage.warning_count(-100, 43) == 0


def test_muted_permissions_disable_every_sending_capability() -> None:
    permissions = muted_permissions()
    sending_fields = (
        "can_send_messages",
        "can_send_audios",
        "can_send_documents",
        "can_send_photos",
        "can_send_videos",
        "can_send_video_notes",
        "can_send_voice_notes",
        "can_send_polls",
        "can_send_other_messages",
        "can_add_web_page_previews",
        "can_react_to_messages",
    )
    assert all(getattr(permissions, field) is False for field in sending_fields)


async def test_manual_commands_allow_unique_authorized_anonymous_admin(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot(anonymous_admin_ids={7873158072})
    controller = ModerationController(
        bot,
        settings(tmp_path, authorized_admin_ids=frozenset({7873158072})),  # type: ignore[arg-type]
        storage,
    )
    chat = SimpleNamespace(id=-100, type=ChatType.SUPERGROUP)

    unauthorized = SimpleNamespace(
        chat=chat,
        from_user=SimpleNamespace(id=42),
        sender_chat=None,
        answer=AsyncMock(),
    )
    assert await controller._group_admin(unauthorized) is None  # type: ignore[arg-type]
    unauthorized.answer.assert_awaited_once()

    anonymous = SimpleNamespace(
        chat=chat,
        from_user=SimpleNamespace(id=1087968824),
        sender_chat=SimpleNamespace(id=-100),
        answer=AsyncMock(),
    )
    anonymous_result = await controller._group_admin(anonymous)  # type: ignore[arg-type]
    assert anonymous_result is not None
    assert anonymous_result[0] == 7873158072
    anonymous.answer.assert_not_awaited()

    authorized = SimpleNamespace(
        chat=chat,
        from_user=SimpleNamespace(id=7873158072),
        sender_chat=None,
        answer=AsyncMock(),
    )
    result = await controller._group_admin(authorized)  # type: ignore[arg-type]
    assert result is not None
    assert result[0] == 7873158072
    authorized.answer.assert_not_awaited()


async def test_manual_commands_reject_ambiguous_anonymous_admins(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot(anonymous_admin_ids={7873158072, 99})
    controller = ModerationController(
        bot,
        settings(tmp_path, authorized_admin_ids=frozenset({7873158072})),  # type: ignore[arg-type]
        storage,
    )
    anonymous = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=1087968824),
        sender_chat=SimpleNamespace(id=-100),
        answer=AsyncMock(),
    )

    assert await controller._group_admin(anonymous) is None  # type: ignore[arg-type]
    anonymous.answer.assert_awaited_once()


async def test_private_admin_command_targets_the_only_allowed_group(
    tmp_path: Path, storage: Storage
) -> None:
    admin_id = 7873158072
    bot = FakeBot(admin_ids={admin_id})
    controller = ModerationController(
        bot,
        settings(tmp_path, authorized_admin_ids=frozenset({admin_id})),  # type: ignore[arg-type]
        storage,
    )
    private_message = SimpleNamespace(
        chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/mute 42 2h 私聊管理测试",
        reply_to_message=None,
        answer=AsyncMock(),
    )

    await controller.mute(private_message)  # type: ignore[arg-type]

    assert bot.restricted == [(-100, 42)]
    private_message.answer.assert_awaited_once_with("已禁言 <code>42</code> 2 小时。")
    stats = await storage.chat_stats(-100)
    assert stats.muted == 1
    assert stats.total_actions == 1


async def test_private_command_rejects_unlisted_user(tmp_path: Path, storage: Storage) -> None:
    controller = ModerationController(
        FakeBot(admin_ids={42}),
        settings(tmp_path, authorized_admin_ids=frozenset({7873158072})),  # type: ignore[arg-type]
        storage,
    )
    private_message = SimpleNamespace(
        chat=SimpleNamespace(id=42, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=42),
        sender_chat=None,
        answer=AsyncMock(),
    )

    assert await controller._group_admin(private_message) is None  # type: ignore[arg-type]
    private_message.answer.assert_awaited_once()


@pytest.mark.parametrize("command", ("/start", "/help"))
async def test_private_router_guard_silently_consumes_unauthorized_commands(
    tmp_path: Path, storage: Storage, command: str
) -> None:
    controller = ModerationController(
        FakeBot(admin_ids={7}),
        settings(tmp_path, authorized_admin_ids=frozenset({7})),  # type: ignore[arg-type]
        storage,
    )
    unauthorized = SimpleNamespace(
        chat=SimpleNamespace(id=42, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=42),
        text=command,
        answer=AsyncMock(),
    )

    assert await controller._unauthorized_private_message(unauthorized) is True  # type: ignore[arg-type]
    await controller._ignore_unauthorized_private(unauthorized)  # type: ignore[arg-type]
    unauthorized.answer.assert_not_awaited()


async def test_private_router_guard_allows_only_the_single_authorized_admin(
    tmp_path: Path, storage: Storage
) -> None:
    controller = ModerationController(
        FakeBot(admin_ids={7}),
        settings(tmp_path, authorized_admin_ids=frozenset({7})),  # type: ignore[arg-type]
        storage,
    )
    authorized = SimpleNamespace(
        chat=SimpleNamespace(id=7, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=7),
        text="/help",
    )

    assert await controller._unauthorized_private_message(authorized) is False  # type: ignore[arg-type]


async def test_private_commands_fail_closed_when_multiple_admins_are_configured(
    tmp_path: Path, storage: Storage
) -> None:
    controller = ModerationController(
        FakeBot(admin_ids={7, 8}),
        settings(tmp_path, authorized_admin_ids=frozenset({7, 8})),  # type: ignore[arg-type]
        storage,
    )
    private_message = SimpleNamespace(
        chat=SimpleNamespace(id=7, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=7),
        sender_chat=None,
        answer=AsyncMock(),
    )

    assert await controller._group_admin(private_message) is None  # type: ignore[arg-type]
    private_message.answer.assert_awaited_once_with("私聊管理要求恰好配置一位授权管理员。")


async def test_manual_ban_and_kick_ignore_legacy_reason_text(
    tmp_path: Path, storage: Storage
) -> None:
    admin_id = 7873158072
    bot = FakeBot(admin_ids={admin_id})
    controller = ModerationController(
        bot,
        settings(tmp_path, authorized_admin_ids=frozenset({admin_id})),  # type: ignore[arg-type]
        storage,
    )
    ban_message = SimpleNamespace(
        chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/ban 42 旧原因文本",
        reply_to_message=None,
        answer=AsyncMock(),
    )
    kick_message = SimpleNamespace(
        chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/kick 43 旧原因文本",
        reply_to_message=None,
        answer=AsyncMock(),
    )

    await controller.ban(ban_message)  # type: ignore[arg-type]
    await controller.kick(kick_message)  # type: ignore[arg-type]

    assert bot.banned == [(-100, 42, True), (-100, 43, False)]
    assert bot.unbanned == [(-100, 43)]
    ban_message.answer.assert_awaited_once_with("已封禁 <code>42</code>。")
    kick_message.answer.assert_awaited_once_with("已移出 <code>43</code>。")
    rows = await storage.db.execute_fetchall(
        "SELECT reason FROM moderation_log ORDER BY id",
    )
    assert [row["reason"] for row in rows] == ["", ""]


async def test_group_warning_message_has_no_reason(tmp_path: Path, storage: Storage) -> None:
    admin_id = 7
    bot = FakeBot(admin_ids={admin_id})
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    target = SimpleNamespace(id=42, full_name="晴天")
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/warn 旧原因文本",
        reply_to_message=SimpleNamespace(from_user=target),
        answer=AsyncMock(),
    )

    await controller.warn(message)  # type: ignore[arg-type]

    message.answer.assert_awaited_once_with('已警告 <a href="tg://user?id=42">晴天</a>（1/3）。')


def test_stats_cutoffs_use_beijing_calendar_days() -> None:
    now = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
    today, week = stats_cutoffs(now)
    assert today == "2026-08-26T16:00:00+00:00"
    assert week == "2026-08-20T16:00:00+00:00"


async def test_recent_is_private_only_and_contains_no_reason(
    tmp_path: Path, storage: Storage
) -> None:
    admin_id = 7
    bot = FakeBot(admin_ids={admin_id})
    controller = ModerationController(
        bot,
        settings(tmp_path, authorized_admin_ids=frozenset({admin_id})),  # type: ignore[arg-type]
        storage,
    )
    await storage.record_action(chat_id=-100, action="ban", actor_id=admin_id, target_id=42)
    group = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/recent",
        answer=AsyncMock(),
    )
    private = SimpleNamespace(
        chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/recent 1",
        answer=AsyncMock(),
    )

    await controller.recent(group)  # type: ignore[arg-type]
    await controller.recent(private)  # type: ignore[arg-type]

    group.answer.assert_awaited_once_with("为避免公开用户 ID，请私聊机器人使用 /recent。")
    output = private.answer.await_args.args[0]
    assert "封禁" in output
    assert "42" in output
    assert "原因" not in output


async def test_recent_rejects_limit_outside_range(tmp_path: Path, storage: Storage) -> None:
    admin_id = 7
    controller = ModerationController(
        FakeBot(admin_ids={admin_id}),
        settings(tmp_path, authorized_admin_ids=frozenset({admin_id})),  # type: ignore[arg-type]
        storage,
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/recent 31",
        answer=AsyncMock(),
    )

    await controller.recent(message)  # type: ignore[arg-type]

    message.answer.assert_awaited_once_with("最近操作数量必须在 1–30 之间。")


async def test_manual_word_addition_is_disabled_in_ai_mode(
    tmp_path: Path, storage: Storage
) -> None:
    admin_id = 7
    controller = ModerationController(
        FakeBot(admin_ids={admin_id}),
        settings(tmp_path, authorized_admin_ids=frozenset({admin_id})),  # type: ignore[arg-type]
        storage,
    )
    add = SimpleNamespace(
        chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text='/addword 词语一，"guaranteed profit"\n词语二',
        answer=AsyncMock(),
    )
    await controller.add_word(add)  # type: ignore[arg-type]

    assert await storage.blocked_words(-100) == set()
    add.answer.assert_awaited_once_with("当前使用 AI 自动学习模式，不接受人工添加拦截词。")


async def test_chunked_list_stays_under_telegram_limit(tmp_path: Path, storage: Storage) -> None:
    controller = ModerationController(FakeBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    message = SimpleNamespace(answer=AsyncMock())
    items = [f"拦截词{index}-" + "长" * 60 for index in range(100)]

    await controller._send_chunked_list(  # type: ignore[arg-type]
        message,
        title="拦截词",
        items=items,
    )

    assert message.answer.await_count > 1
    assert all(len(call.args[0]) < 4096 for call in message.answer.await_args_list)
    assert all("拦截词（100）" in call.args[0] for call in message.answer.await_args_list)


async def test_inline_list_displays_words_horizontally(tmp_path: Path, storage: Storage) -> None:
    controller = ModerationController(FakeBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    message = SimpleNamespace(answer=AsyncMock())

    await controller._send_chunked_list(  # type: ignore[arg-type]
        message,
        title="拦截词",
        items=["号池", "供应", "合作", "长期"],
        inline=True,
    )

    output = message.answer.await_args.args[0]
    assert output == (
        "<b>拦截词（4）</b>\n"
        "<code>号池</code> · <code>供应</code> · "
        "<code>合作</code> · <code>长期</code>"
    )
    assert "•" not in output


async def test_reporter_log_message_contains_no_reason(
    tmp_path: Path, storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, log_chat_id=999),  # type: ignore[arg-type]
        storage,
    )

    await controller.reporter.record(
        chat_id=-100,
        action="ban",
        actor_id=7,
        target_id=42,
    )

    assert len(bot.sent) == 1
    assert "原因" not in bot.sent[0][1]
    assert all("reason=" not in record.getMessage() for record in caplog.records)


async def test_ai_normal_chat_is_reviewed_silently(tmp_path: Path, storage: Storage) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.99,
                category="normal",
                signatures=(),
                cached=False,
                latency_ms=25,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "请问这个商品怎么购买？"))

    reviewer.review.assert_awaited_once()
    controller._processing_notice.assert_not_awaited()
    controller._notice.assert_not_awaited()
    assert bot.deleted == []
    stats = await storage.ai_stats(-100)
    assert stats.total == 1
    assert stats.ads == 0


async def test_member_command_is_moderated_before_command_handlers(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.99,
                category="normal",
                signatures=(),
                cached=False,
                latency_ms=10,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )

    private_guard, member_command_guard = controller.router.message.handlers[:2]
    assert private_guard.callback == controller._ignore_unauthorized_private
    assert member_command_guard.callback == controller.moderate_message
    await controller.router.propagate_event(
        "message",
        group_message(42, "/help 请联系这个账号"),
        bot=bot,
    )

    reviewer.review.assert_awaited_once()


async def test_member_command_guard_exempts_admin_trusted_and_anonymous_admin(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot(admin_ids={42})
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    await storage.add_trusted_user(-100, 43, 42)

    assert await controller._ordinary_member_command(group_message(42, "/help")) is False
    assert await controller._ordinary_member_command(group_message(43, "/help")) is False
    anonymous = group_message(44, "/help").model_copy(
        update={"sender_chat": Chat(id=-100, type=ChatType.SUPERGROUP, title="Test")}
    )
    assert await controller._ordinary_member_command(anonymous) is False
    assert await controller._ordinary_member_command(group_message(44, "/help")) is True


async def test_edited_message_is_registered_for_ai_without_counting_flood(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.99,
                category="normal",
                signatures=(),
                cached=False,
                latency_ms=10,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller.flood.check = AsyncMock()  # type: ignore[method-assign]
    edited = group_message(42, "编辑后的普通消息").model_copy(
        update={"edit_date": datetime.now(UTC)}
    )

    assert controller.router.edited_message.handlers[0].callback == controller.moderate_message
    await controller.router.propagate_event("edited_message", edited, bot=bot)

    reviewer.review.assert_awaited_once()
    controller.flood.check.assert_not_awaited()  # type: ignore[attr-defined]


async def test_edited_message_changed_to_ad_is_deleted(tmp_path: Path, storage: Storage) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=True,
                confidence=0.95,
                category="promotion",
                signatures=(),
                cached=False,
                latency_ms=10,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._notice = AsyncMock()
    edited = group_message(42, "编辑后的广告内容").model_copy(
        update={"edit_date": datetime.now(UTC)}
    )

    await controller.router.propagate_event("edited_message", edited, bot=bot)

    reviewer.review.assert_awaited_once()
    assert bot.deleted == [(-100, 1)]
    assert await storage.warning_count(-100, 42) == 1


async def test_ai_exempts_admin_trusted_and_linked_channel_messages(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(review=AsyncMock())
    bot = FakeBot(admin_ids={42})
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    await storage.add_trusted_user(-100, 43, 42)

    await controller.moderate_message(group_message(42, "管理员消息"))
    await controller.moderate_message(group_message(43, "可信用户消息", message_id=2))
    chat = SimpleNamespace(id=-100, type=ChatType.SUPERGROUP)
    await controller.moderate_message(  # type: ignore[arg-type]
        SimpleNamespace(chat=chat, is_automatic_forward=True)
    )
    await controller.moderate_message(  # type: ignore[arg-type]
        SimpleNamespace(
            chat=chat,
            is_automatic_forward=False,
            sender_chat=SimpleNamespace(id=-100),
        )
    )

    reviewer.review.assert_not_awaited()


async def test_ai_suspicious_ad_shows_review_and_records_warning(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=True,
                confidence=0.98,
                category="crypto_ad",
                signatures=("免费领取 usdt",),
                cached=False,
                latency_ms=50,
                rejected_signatures=("合作",),
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "免费领取 USDT，访问 https://evil.example"))

    controller._processing_notice.assert_awaited_once()
    assert bot.deleted == [(-100, 1)]
    assert await storage.warning_count(-100, 42) == 1
    moderation = await storage.chat_stats(-100)
    assert moderation.deleted == 1
    assert moderation.warned == 1
    signatures = await storage.active_ai_signatures(-100)
    assert "免费领取 usdt" in signatures
    rejected = await storage.ai_signatures(-100, statuses=("rejected",))
    assert [item.display_signature for item in rejected] == ["合作"]
    assert "原因" not in controller._notice.await_args.args[1]


async def test_every_successful_ad_persists_safe_candidates_before_enforcement(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=True,
                confidence=0.80,
                category="promotion",
                signatures=("免费领取 usdt",),
                cached=False,
                latency_ms=25,
                rejected_signatures=("合作",),
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)

    await controller.moderate_message(group_message(42, "免费领取 USDT，合作", message_id=1))
    await controller.moderate_message(group_message(42, "免费领取 USDT，合作", message_id=2))

    assert bot.deleted == []
    assert await storage.warning_count(-100, 42) == 0
    candidates = await storage.ai_signatures(-100, statuses=("candidate",))
    assert [(item.display_signature, item.hit_count) for item in candidates] == [
        ("免费领取 usdt", 2)
    ]
    rejected = await storage.ai_signatures(-100, statuses=("rejected",))
    assert [(item.display_signature, item.hit_count) for item in rejected] == [("合作", 2)]
    decisions = await storage.recent_ai_decisions(-100, limit=2)
    assert len(decisions) == 2
    assert all(item.is_ad is True and item.enforced is False for item in decisions)
    learning = await storage.ai_learning_memory(-100)
    assert [(item.evidence_profile, item.decision_count) for item in learning] == [
        ("text,promotion", 2)
    ]


async def test_normal_ai_decision_never_persists_signatures(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.10,
                category="normal",
                signatures=("免费领取 usdt",),
                cached=False,
                latency_ms=15,
                rejected_signatures=("合作",),
            )
        )
    )
    controller = ModerationController(
        FakeBot(),
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)

    await controller.moderate_message(group_message(42, "普通咨询", message_id=1))

    assert await storage.ai_signatures(-100, statuses=("candidate", "active", "rejected")) == ()
    assert await storage.ai_learning_memory(-100) == ()


async def test_repeated_enforced_candidate_becomes_active_after_three_ad_decisions(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=True,
                confidence=0.93,
                category="promotion",
                signatures=("免费领取 usdt",),
                cached=False,
                latency_ms=20,
            )
        )
    )
    controller = ModerationController(
        FakeBot(),
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    controller._notice = AsyncMock()

    for message_id in range(1, 4):
        await controller.moderate_message(group_message(42, "免费领取 USDT", message_id=message_id))

    assert await storage.active_ai_signatures(-100) == {"免费领取 usdt"}
    signatures = await storage.ai_signatures(-100)
    assert signatures[0].hit_count == 3


async def test_hidden_text_link_host_is_visible_to_ai_without_path_or_query(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.10,
                category="normal",
                signatures=(),
                cached=False,
                latency_ms=12,
            )
        )
    )
    controller = ModerationController(
        FakeBot(),
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        from_user=User(id=42, is_bot=False, first_name="Member"),
        text="官网",
        entities=[
            MessageEntity(
                type=MessageEntityType.TEXT_LINK,
                offset=0,
                length=2,
                url="https://example.org.evil.example/private?token=do-not-forward",
            )
        ],
    )

    await controller.moderate_message(message)

    assert reviewer.review.await_args.kwargs["linked_hosts"] == ("example.org.evil.example",)
    assert reviewer.review.await_args.kwargs["risk_signals"] == ("link",)
    assert "private" not in repr(reviewer.review.await_args.kwargs)
    assert "do-not-forward" not in repr(reviewer.review.await_args.kwargs)


async def test_image_ad_without_safe_signature_still_updates_permanent_learning(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=True,
                confidence=0.99,
                category="promotion",
                signatures=(),
                cached=False,
                latency_ms=18,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._message_image_data_url = AsyncMock(return_value="data:image/jpeg;base64,YWQ=")
    controller._notice = AsyncMock()

    await controller.moderate_message(group_photo_message(42, message_id=1))

    assert bot.deleted == [(-100, 1)]
    assert await storage.ai_signatures(-100, statuses=("candidate", "active", "rejected")) == ()
    learning = await storage.ai_learning_memory(-100)
    assert [(item.evidence_profile, item.decision_count) for item in learning] == [("image", 1)]


async def test_ai_ad_missed_by_precheck_is_deleted_with_separate_result(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=True,
                confidence=0.93,
                category="promotion",
                signatures=(),
                cached=False,
                latency_ms=30,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "今天有个特别活动"))

    controller._processing_notice.assert_not_awaited()
    assert bot.deleted == [(-100, 1)]
    controller._notice.assert_awaited_once_with(ANY, "广告内容已删除，警告 1/3。")


async def test_active_ai_signature_bypasses_gateway(tmp_path: Path, storage: Storage) -> None:
    await storage.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="免费领取 usdt",
        display_signature="免费领取 USDT",
        category="crypto_ad",
        status="active",
        confidence=0.99,
    )
    reviewer = SimpleNamespace(review=AsyncMock())
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "免费 领取 USDT 现在参加"))

    reviewer.review.assert_not_awaited()
    assert bot.deleted == [(-100, 1)]
    decisions = await storage.recent_ai_decisions(-100, limit=1)
    assert decisions[0].model == "local-signature"
    assert decisions[0].enforced is True
    signatures = await storage.ai_signatures(-100)
    assert signatures[0].hit_count == 2


async def test_ai_failure_deletes_without_warning(tmp_path: Path, storage: Storage) -> None:
    reviewer = SimpleNamespace(review=AsyncMock(side_effect=AIReviewTimeout("timeout")))
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True, ai_failure_delete=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "普通消息"))

    assert bot.deleted == [(-100, 1)]
    assert await storage.warning_count(-100, 42) == 0
    assert (await storage.chat_stats(-100)).warned == 0
    assert (await storage.ai_stats(-100)).failed == 1
    controller._notice.assert_awaited_once_with(ANY, "审核服务异常，消息已删除。")


async def test_private_ai_management_commands(tmp_path: Path, storage: Storage) -> None:
    admin_id = 7
    controller = ModerationController(
        FakeBot(admin_ids={admin_id}),
        settings(tmp_path, authorized_admin_ids=frozenset({admin_id})),  # type: ignore[arg-type]
        storage,
    )
    active = await storage.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="免费领取 usdt",
        display_signature="免费领取 USDT",
        category="crypto_ad",
        status="active",
        confidence=0.99,
    )
    await storage.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="扫码进入广告群",
        display_signature="扫码进入广告群",
        category="group_ad",
        status="candidate",
        confidence=0.94,
    )
    await storage.record_ai_decision(
        chat_id=-100,
        message_id=1,
        target_id=42,
        is_ad=True,
        confidence=0.98,
        category="crypto_ad",
        model=AI_MODEL,
        latency_ms=40,
        cached=False,
        enforced=True,
    )

    def private(text: str) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
            from_user=SimpleNamespace(id=admin_id),
            sender_chat=None,
            text=text,
            answer=AsyncMock(),
        )

    words = private("/words")
    candidates = private("/aicandidates")
    stats = private("/aistats")
    recent = private("/airecent 1")
    pause = private("/aipause")
    resume = private("/airesume")
    delete = private(f"/delword {active.id}")

    await controller.list_words(words)  # type: ignore[arg-type]
    await controller.ai_candidates(candidates)  # type: ignore[arg-type]
    await controller.ai_stats(stats)  # type: ignore[arg-type]
    await controller.ai_recent(recent)  # type: ignore[arg-type]
    await controller.ai_pause(pause)  # type: ignore[arg-type]
    assert await storage.runtime_setting(-100, "ai_paused") == "1"
    await controller.ai_resume(resume)  # type: ignore[arg-type]
    assert await storage.runtime_setting(-100, "ai_paused") == "0"
    await controller.delete_word(delete)  # type: ignore[arg-type]

    assert "AI 已学习特征（1）" in words.answer.await_args.args[0]
    assert "扫码进入广告群" in candidates.answer.await_args.args[0]
    assert "判断 1｜调用 1" in stats.answer.await_args.args[0]
    assert "失败率 0.0%" in stats.answer.await_args.args[0]
    assert "广告" in recent.answer.await_args.args[0]
    assert AI_MODEL not in recent.answer.await_args.args[0]
    delete.answer.assert_awaited_once_with("已停用该 AI 特征。")
    assert await storage.active_ai_signatures(-100) == set()


async def test_ai_pause_skips_gateway_and_processing(tmp_path: Path, storage: Storage) -> None:
    reviewer = SimpleNamespace(review=AsyncMock())
    controller = ModerationController(
        FakeBot(),
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    await storage.set_runtime_setting(-100, "ai_paused", "1")

    await controller.moderate_message(group_message(42, "https://evil.example"))

    reviewer.review.assert_not_awaited()
    controller._processing_notice.assert_not_awaited()


async def test_processing_notice_is_edited_without_new_notice(
    tmp_path: Path, storage: Storage
) -> None:
    controller = ModerationController(FakeBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    source = group_message(42, "可疑消息")
    processing = SimpleNamespace(
        chat=SimpleNamespace(id=-100),
        message_id=9,
        edit_text=AsyncMock(),
    )
    controller._notice = AsyncMock()

    await controller._finish_processing(  # type: ignore[arg-type]
        source, processing, "审核完成。"
    )

    processing.edit_text.assert_awaited_once_with("审核完成。")
    controller._notice.assert_not_awaited()


async def test_photo_is_loaded_as_bounded_data_url(tmp_path: Path, storage: Storage) -> None:
    bot = FakeBot()

    async def download(_file: object, *, destination: object) -> None:
        destination.write(b"jpeg-bytes")

    bot.download = AsyncMock(side_effect=download)  # type: ignore[attr-defined]
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_size=10)],
        video=None,
    )

    result = await controller._message_image_data_url(message)  # type: ignore[arg-type]

    assert result == "data:image/jpeg;base64,anBlZy1ieXRlcw=="
    bot.download.assert_awaited_once()  # type: ignore[attr-defined]

    message.photo[0].file_size = controller.settings.ai_image_max_bytes + 1
    bot.download.reset_mock()  # type: ignore[attr-defined]
    assert await controller._message_image_data_url(message) is None  # type: ignore[arg-type]
    bot.download.assert_not_awaited()  # type: ignore[attr-defined]


async def test_video_uses_thumbnail_without_downloading_video(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot()

    async def download(file: object, *, destination: object) -> None:
        assert file is thumbnail
        destination.write(b"thumbnail")

    thumbnail = SimpleNamespace(file_size=9)
    bot.download = AsyncMock(side_effect=download)  # type: ignore[attr-defined]
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    message = SimpleNamespace(
        photo=None,
        video=SimpleNamespace(thumbnail=thumbnail, file_size=50_000_000),
    )

    result = await controller._message_image_data_url(message)  # type: ignore[arg-type]

    assert result == "data:image/jpeg;base64,dGh1bWJuYWls"
    bot.download.assert_awaited_once_with(thumbnail, destination=ANY)  # type: ignore[attr-defined]


async def test_ai_mode_sends_flood_signal_to_gateway_without_local_enforcement(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.99,
                category="normal",
                signatures=(),
                cached=False,
                latency_ms=10,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(
            tmp_path,
            ai_enabled=True,
            flood_max_messages=1,
            duplicate_max_messages=99,
        ),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "第一条正常消息", message_id=1))
    await controller.moderate_message(group_message(42, "第二条正常消息", message_id=2))

    assert reviewer.review.await_count == 2
    assert reviewer.review.await_args_list[0].kwargs["risk_signals"] == ()
    assert reviewer.review.await_args_list[1].kwargs["risk_signals"] == ("flood",)
    assert bot.deleted == []
    assert await storage.warning_count(-100, 42) == 0
    controller._processing_notice.assert_awaited_once()


async def test_ai_mode_sends_duplicate_signal_to_gateway_without_local_enforcement(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.99,
                category="normal",
                signatures=(),
                cached=False,
                latency_ms=10,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(
            tmp_path,
            ai_enabled=True,
            flood_max_messages=99,
            duplicate_max_messages=1,
        ),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)

    await controller.moderate_message(group_message(42, "相同普通消息", message_id=1))
    await controller.moderate_message(group_message(42, "相同普通消息", message_id=2))

    assert reviewer.review.await_count == 2
    assert reviewer.review.await_args_list[1].kwargs["risk_signals"] == ("duplicate",)
    assert bot.deleted == []
    assert await storage.warning_count(-100, 42) == 0
    controller._processing_notice.assert_awaited_once()


async def test_concurrent_threshold_hits_only_ban_and_clear_once(
    tmp_path: Path, storage: Storage
) -> None:
    class BlockingBanBot(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.ban_started = asyncio.Event()
            self.release_ban = asyncio.Event()

        async def ban_chat_member(
            self, chat_id: int, user_id: int, *, revoke_messages: bool
        ) -> bool:
            self.banned.append((chat_id, user_id, revoke_messages))
            if len(self.banned) == 1:
                self.ban_started.set()
                await self.release_ban.wait()
            return True

    bot = BlockingBanBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, warn_limit=1),  # type: ignore[arg-type]
        storage,
    )
    controller._notice = AsyncMock()

    first = asyncio.create_task(
        controller._handle_violation(
            group_message(42, "广告一", message_id=1),
            42,
            Violation("test", ""),
        )
    )
    await bot.ban_started.wait()
    second = asyncio.create_task(
        controller._handle_violation(
            group_message(42, "广告二", message_id=2),
            42,
            Violation("test", ""),
        )
    )
    await asyncio.sleep(0)
    assert len(bot.banned) == 1

    bot.release_ban.set()
    await asyncio.gather(first, second)

    assert bot.banned == [(-100, 42, True)]
    assert bot.deleted == [(-100, 1), (-100, 2)]
    assert await storage.warning_count(-100, 42) == 0
    stats = await storage.chat_stats(-100)
    assert stats.warned == 1
    assert stats.banned == 1


async def test_auto_ban_failure_is_reported_without_a_reason(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot()
    bot.ban_chat_member = AsyncMock(  # type: ignore[method-assign]
        side_effect=TelegramBadRequest(
            method=BanChatMember(chat_id=-100, user_id=42),
            message="permission denied",
        )
    )
    controller = ModerationController(
        bot,
        settings(tmp_path, warn_limit=1),  # type: ignore[arg-type]
        storage,
    )
    controller._notice = AsyncMock()

    await controller._handle_violation(
        group_message(42, "广告", message_id=1),
        42,
        Violation("test", ""),
    )

    controller._notice.assert_awaited_once_with(ANY, "广告内容已删除，自动封禁失败。")
    assert "原因" not in controller._notice.await_args.args[1]


async def test_member_action_state_is_bounded(tmp_path: Path, storage: Storage) -> None:
    controller = ModerationController(FakeBot(), settings(tmp_path), storage)  # type: ignore[arg-type]

    lock = controller._member_action_lock(-100, 1)
    assert len(controller._member_action_locks) == 1
    del lock
    gc.collect()
    assert len(controller._member_action_locks) == 0

    for user_id in range(AUTO_BAN_MEMORY_MAX + 5):
        controller._remember_auto_banned((-100, user_id))
    assert len(controller._auto_banned_members) == AUTO_BAN_MEMORY_MAX


async def test_pause_while_ai_is_in_flight_preserves_learning_but_prevents_enforcement(
    tmp_path: Path, storage: Storage
) -> None:
    review_started = asyncio.Event()
    release_review = asyncio.Event()

    async def review(**_kwargs: object) -> AIReviewResult:
        review_started.set()
        await release_review.wait()
        return AIReviewResult(
            is_ad=True,
            confidence=0.99,
            category="promotion",
            signatures=("免费领取 usdt",),
            cached=False,
            latency_ms=20,
        )

    reviewer = SimpleNamespace(review=AsyncMock(side_effect=review))
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    processing = SimpleNamespace(
        chat=SimpleNamespace(id=-100),
        message_id=99,
        edit_text=AsyncMock(),
    )
    controller._processing_notice = AsyncMock(return_value=processing)

    task = asyncio.create_task(
        controller.moderate_message(group_message(42, "免费领取 USDT", message_id=1))
    )
    await review_started.wait()
    await storage.set_runtime_setting(-100, "ai_paused", "1")
    release_review.set()
    await task

    assert bot.deleted == []
    assert await storage.warning_count(-100, 42) == 0
    assert await storage.active_ai_signatures(-100) == {"免费领取 usdt"}
    decisions = await storage.recent_ai_decisions(-100, limit=1)
    assert decisions[0].enforced is False
    processing.edit_text.assert_awaited_once_with("AI 审核已暂停，消息已保留。")


async def test_pause_while_gateway_fails_prevents_failure_delete(
    tmp_path: Path, storage: Storage
) -> None:
    review_started = asyncio.Event()
    release_review = asyncio.Event()

    async def review(**_kwargs: object) -> AIReviewResult:
        review_started.set()
        await release_review.wait()
        raise AIReviewTimeout("timeout")

    reviewer = SimpleNamespace(review=AsyncMock(side_effect=review))
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True, ai_failure_delete=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    processing = SimpleNamespace(
        chat=SimpleNamespace(id=-100),
        message_id=99,
        edit_text=AsyncMock(),
    )
    controller._processing_notice = AsyncMock(return_value=processing)

    task = asyncio.create_task(
        controller.moderate_message(group_message(42, "https://evil.example", message_id=1))
    )
    await review_started.wait()
    await storage.set_runtime_setting(-100, "ai_paused", "1")
    release_review.set()
    await task

    assert bot.deleted == []
    assert await storage.warning_count(-100, 42) == 0
    processing.edit_text.assert_awaited_once_with("AI 审核已暂停，消息已保留。")


async def test_cached_success_does_not_hide_consecutive_gateway_failures(
    tmp_path: Path, storage: Storage
) -> None:
    cached_normal = AIReviewResult(
        is_ad=False,
        confidence=0.99,
        category="normal",
        signatures=(),
        cached=True,
        latency_ms=0,
    )
    live_normal = AIReviewResult(
        is_ad=False,
        confidence=0.99,
        category="normal",
        signatures=(),
        cached=False,
        latency_ms=10,
    )
    reviewer = SimpleNamespace(
        review=AsyncMock(
            side_effect=[
                AIReviewTimeout("one"),
                cached_normal,
                AIReviewTimeout("two"),
                cached_normal,
                AIReviewTimeout("three"),
                live_normal,
            ]
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(
            tmp_path,
            ai_enabled=True,
            authorized_admin_ids=frozenset({7}),
        ),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._notice = AsyncMock()

    for message_id in range(1, 6):
        await controller.moderate_message(
            group_message(42 + message_id, f"普通消息 {message_id}", message_id=message_id)
        )

    assert bot.sent == [(7, "AI 审核已连续失败 3 次，请立即检查网关。")]
    assert controller._ai_failure_streak == 3

    await controller.moderate_message(group_message(99, "恢复消息", message_id=6))
    assert controller._ai_failure_streak == 0


async def test_unavailable_photo_falls_back_to_caption_without_failure_policy(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot()
    bot.download = AsyncMock(side_effect=OSError("download failed"))  # type: ignore[attr-defined]
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.02,
                category="normal",
                signatures=(),
                cached=False,
                latency_ms=10,
            )
        )
    )
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True, ai_failure_delete=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)

    await controller.moderate_message(group_photo_message(42, caption="正常图片说明", message_id=1))

    assert reviewer.review.await_count == 1
    assert reviewer.review.await_args.kwargs["text"] == "正常图片说明"
    assert reviewer.review.await_args.kwargs["image_data_url"] is None
    assert controller._ai_failure_streak == 0
    assert bot.deleted == []
    assert await storage.warning_count(-100, 42) == 0
    decision = (await storage.recent_ai_decisions(-100, limit=1))[0]
    assert decision.is_ad is False
    assert decision.error_type == ""


async def test_unavailable_photo_without_caption_is_silently_retained(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot()
    bot.download = AsyncMock(side_effect=OSError("download failed"))  # type: ignore[attr-defined]
    reviewer = SimpleNamespace(review=AsyncMock())
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True, ai_failure_delete=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)

    await controller.moderate_message(group_photo_message(42, message_id=1))

    reviewer.review.assert_not_awaited()
    controller._processing_notice.assert_not_awaited()
    assert bot.deleted == []
    assert await storage.recent_ai_decisions(-100, limit=1) == ()


async def test_aiohttp_payload_error_uses_gateway_failure_policy(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(review=AsyncMock(side_effect=ClientPayloadError("broken body")))
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(tmp_path, ai_enabled=True, ai_failure_delete=True),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "普通消息"))

    assert bot.deleted == [(-100, 1)]
    decision = (await storage.recent_ai_decisions(-100, limit=1))[0]
    assert decision.error_type == "ClientPayloadError"


async def test_dry_run_ai_failure_reports_retention_not_delete_failure(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(review=AsyncMock(side_effect=AIReviewTimeout("timeout")))
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(
            tmp_path,
            ai_enabled=True,
            ai_failure_delete=True,
            dry_run=True,
        ),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "普通消息"))

    assert bot.deleted == []
    controller._notice.assert_awaited_once_with(
        ANY,
        "审核服务异常，观察模式下消息已保留。",
    )
