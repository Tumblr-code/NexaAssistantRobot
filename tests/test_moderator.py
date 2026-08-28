from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import Chat, Message, User

from nexa_guardian.config import Settings
from nexa_guardian.moderator import ModerationController, muted_permissions, stats_cutoffs
from nexa_guardian.storage import Storage


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

    async def ban_chat_member(
        self, chat_id: int, user_id: int, *, revoke_messages: bool
    ) -> bool:
        self.banned.append((chat_id, user_id, revoke_messages))
        return True

    async def unban_chat_member(
        self, chat_id: int, user_id: int, *, only_if_banned: bool
    ) -> bool:
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
    notice = controller._notice.await_args.args[1]
    assert "并封禁" in notice
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


async def test_private_command_rejects_unlisted_user(
    tmp_path: Path, storage: Storage
) -> None:
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

    message.answer.assert_awaited_once_with(
        '已警告 <a href="tg://user?id=42">晴天</a>（1/3）。'
    )


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


async def test_batch_word_commands_are_atomic(tmp_path: Path, storage: Storage) -> None:
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
    invalid = SimpleNamespace(
        chat=SimpleNamespace(id=admin_id, type=ChatType.PRIVATE),
        from_user=SimpleNamespace(id=admin_id),
        sender_chat=None,
        text="/addword 有效词，短",
        answer=AsyncMock(),
    )

    await controller.add_word(add)  # type: ignore[arg-type]
    await controller.add_word(invalid)  # type: ignore[arg-type]

    assert await storage.blocked_words(-100) == {"词语一", "guaranteed profit", "词语二"}
    assert "已添加 3 条" in add.answer.await_args.args[0]
    invalid.answer.assert_awaited_once_with("所有拦截词长度必须为 2–64 个字符，本次未添加。")


async def test_chunked_list_stays_under_telegram_limit(
    tmp_path: Path, storage: Storage
) -> None:
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


async def test_inline_list_displays_words_horizontally(
    tmp_path: Path, storage: Storage
) -> None:
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
