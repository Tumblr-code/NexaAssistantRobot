import asyncio
import gc
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import BanChatMember, DeleteMessage
from aiogram.types import Chat, Message, MessageEntity, PhotoSize, User, Video
from aiohttp import ClientPayloadError

import nexa_guardian.moderator as moderator_module
from nexa_guardian import branding
from nexa_guardian.ai_review import AIReviewResult, AIReviewTimeout
from nexa_guardian.config import Settings
from nexa_guardian.detection import Violation
from nexa_guardian.moderator import (
    AUTO_BAN_MEMORY_MAX,
    ModerationController,
    select_photo_preview,
)
from nexa_guardian.raffle_service import RaffleService
from nexa_guardian.status_service import stats_cutoffs
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
        self.member_checks: list[tuple[int, int]] = []

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
        self,
        chat_id: int,
        user_id: int,
        *,
        until_date: datetime | None = None,
        revoke_messages: bool,
    ) -> bool:
        self.banned.append((chat_id, user_id, revoke_messages))
        return True

    async def unban_chat_member(self, chat_id: int, user_id: int, *, only_if_banned: bool) -> bool:
        self.unbanned.append((chat_id, user_id))
        return True

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id: int, user_id: int):
        self.member_checks.append((chat_id, user_id))
        status = (
            ChatMemberStatus.CREATOR
            if user_id in self.admin_ids | self.anonymous_admin_ids
            else ChatMemberStatus.MEMBER
        )
        return SimpleNamespace(status=status)


class RaffleBot(FakeBot):
    def __init__(self, *, pin_result: bool = True, anonymous_admin: bool = False) -> None:
        super().__init__(
            admin_ids=set() if anonymous_admin else {7},
            anonymous_admin_ids={7} if anonymous_admin else set(),
        )
        self.pin_result = pin_result
        self.pinned: list[tuple[int, int, bool]] = []
        self.unpinned: list[tuple[int, int]] = []
        self.edited: list[tuple[int, int, str]] = []
        self._next_message_id = 100

    async def send_message(self, chat_id: int, text: str, **kwargs) -> Message:
        self.sent.append((chat_id, text))
        self._next_message_id += 1
        return Message(
            message_id=self._next_message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Test"),
            text=text,
        )

    async def edit_message_text(
        self, text: str, *, chat_id: int, message_id: int, **kwargs
    ) -> Message:
        self.edited.append((chat_id, message_id, text))
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Test"),
            text=text,
        )

    async def pin_chat_message(
        self, chat_id: int, message_id: int, *, disable_notification: bool
    ) -> bool:
        self.pinned.append((chat_id, message_id, disable_notification))
        return self.pin_result

    async def unpin_chat_message(self, chat_id: int, message_id: int) -> bool:
        self.unpinned.append((chat_id, message_id))
        return True


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


async def warning_count(storage: Storage, chat_id: int, user_id: int) -> int:
    cursor = await storage.db.execute(
        "SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id),
    )
    row = await cursor.fetchone()
    return int(row["count"]) if row is not None else 0


async def recent_ai_decisions(
    storage: Storage,
    chat_id: int,
    *,
    limit: int,
) -> tuple[SimpleNamespace, ...]:
    rows = await storage.db.execute_fetchall(
        "SELECT * FROM ai_decisions WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    )
    items: list[SimpleNamespace] = []
    for row in rows:
        values = dict(row)
        values["is_ad"] = None if values["is_ad"] is None else bool(values["is_ad"])
        values["cached"] = bool(values["cached"])
        values["enforced"] = bool(values["enforced"])
        items.append(SimpleNamespace(**values))
    return tuple(items)


async def seed_trusted_user(
    storage: Storage,
    chat_id: int,
    user_id: int,
    added_by: int,
) -> None:
    await storage.db.execute(
        "INSERT INTO trusted_users (chat_id, user_id, added_by, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, added_by, "2026-08-30T00:00:00+00:00"),
    )
    await storage.db.commit()


async def seed_allowed_domain(
    storage: Storage,
    chat_id: int,
    domain: str,
    added_by: int,
) -> None:
    await storage.db.execute(
        "INSERT INTO allowed_domains (chat_id, domain, added_by, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, domain, added_by, "2026-08-30T00:00:00+00:00"),
    )
    await storage.db.commit()


async def test_external_link_is_deleted_and_warned(tmp_path: Path, storage: Storage) -> None:
    bot = FakeBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "https://evil.example"))

    assert bot.deleted == [(-100, 1)]
    assert await warning_count(storage, -100, 42) == 1
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
    assert await warning_count(storage, -100, 42) == 0
    stats = await storage.chat_stats(-100)
    assert stats.banned == 1
    assert stats.warned == 1
    notice = controller._notice.await_args.args[1]
    assert "用户已封禁" in notice
    assert "禁言" not in notice


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
    assert await warning_count(storage, -100, 43) == 0


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


async def test_set_commands_syncs_default_and_chinese_profile(
    tmp_path: Path, storage: Storage
) -> None:
    bot = SimpleNamespace(
        get_my_commands=AsyncMock(return_value=[]),
        get_my_name=AsyncMock(return_value=SimpleNamespace(name="")),
        get_my_short_description=AsyncMock(return_value=SimpleNamespace(short_description="")),
        get_my_description=AsyncMock(return_value=SimpleNamespace(description="")),
        set_my_commands=AsyncMock(),
        set_my_name=AsyncMock(),
        set_my_short_description=AsyncMock(),
        set_my_description=AsyncMock(),
    )
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]

    await controller.set_commands()

    assert [call.kwargs["language_code"] for call in bot.set_my_commands.await_args_list] == [
        None,
        "zh",
    ]
    commands = bot.set_my_commands.await_args_list[0].args[0]
    assert {command.command for command in commands} == {
        "help",
        "status",
        "del",
        "ban",
        "lottery",
    }
    assert bot.set_my_name.await_args_list[0].args == (branding.bot_profile_name(),)
    assert bot.set_my_name.await_args_list[1].kwargs["language_code"] == "zh"
    assert bot.set_my_short_description.await_args_list[0].args == (
        branding.bot_profile_short_description(),
    )
    assert bot.set_my_description.await_args_list[0].args == (branding.bot_profile_description(),)
    assert len(branding.bot_profile_name()) <= 64
    assert len(branding.bot_profile_short_description()) <= 120
    assert len(branding.bot_profile_description()) <= 512


async def test_set_commands_does_not_write_matching_profile(
    tmp_path: Path, storage: Storage
) -> None:
    commands = [
        SimpleNamespace(command="help", description="查看极简使用说明"),
        SimpleNamespace(command="status", description="查看运行状态与管理统计"),
        SimpleNamespace(command="del", description="删除消息、群历史或成员"),
        SimpleNamespace(command="ban", description="回复消息或填写用户 ID 封禁"),
        SimpleNamespace(command="lottery", description="创建或管理关键词抽奖"),
    ]
    bot = SimpleNamespace(
        get_my_commands=AsyncMock(return_value=commands),
        get_my_name=AsyncMock(return_value=SimpleNamespace(name=branding.bot_profile_name())),
        get_my_short_description=AsyncMock(
            return_value=SimpleNamespace(short_description=branding.bot_profile_short_description())
        ),
        get_my_description=AsyncMock(
            return_value=SimpleNamespace(description=branding.bot_profile_description())
        ),
        set_my_commands=AsyncMock(),
        set_my_name=AsyncMock(),
        set_my_short_description=AsyncMock(),
        set_my_description=AsyncMock(),
    )
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]

    await controller.set_commands()

    bot.set_my_commands.assert_not_awaited()
    bot.set_my_name.assert_not_awaited()
    bot.set_my_short_description.assert_not_awaited()
    bot.set_my_description.assert_not_awaited()


async def test_profile_field_failure_does_not_block_other_fields(
    tmp_path: Path, storage: Storage
) -> None:
    bot = SimpleNamespace(
        get_my_commands=AsyncMock(return_value=[]),
        get_my_name=AsyncMock(return_value=SimpleNamespace(name="")),
        get_my_short_description=AsyncMock(return_value=SimpleNamespace(short_description="")),
        get_my_description=AsyncMock(return_value=SimpleNamespace(description="")),
        set_my_commands=AsyncMock(),
        set_my_name=AsyncMock(
            side_effect=TelegramBadRequest(
                method=DeleteMessage(chat_id=1, message_id=1),
                message="profile update rejected",
            )
        ),
        set_my_short_description=AsyncMock(),
        set_my_description=AsyncMock(),
    )
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]

    await controller.set_commands()

    assert bot.set_my_name.await_count == 2
    assert bot.set_my_short_description.await_count == 2
    assert bot.set_my_description.await_count == 2


async def test_lottery_creation_pins_keyword_card_and_activates(
    tmp_path: Path, storage: Storage
) -> None:
    bot = RaffleBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    controller._group_admin = AsyncMock(return_value=(7, SimpleNamespace(), -100))  # type: ignore[method-assign]
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=7),
        text="/lottery 30m 1 ＮＥＸＡ GiftPass 月卡",
        answer=AsyncMock(),
    )

    await controller.lottery(message)  # type: ignore[arg-type]

    raffle = await storage.active_raffle(-100)
    assert raffle is not None
    assert raffle.keyword == "NEXA"
    assert raffle.normalized_keyword == "nexa"
    assert raffle.publication_state == "active"
    assert bot.pinned == [(-100, raffle.message_id, True)]
    assert "参与关键词：<code>NEXA</code>" in bot.sent[0][1]
    message.answer.assert_awaited_once_with(
        "🎉 <b>抽奖已发布并置顶</b>\n发送关键词 <code>NEXA</code> 即可参与。"
    )


async def test_lottery_pin_failure_deletes_card_and_rolls_back(
    tmp_path: Path, storage: Storage
) -> None:
    bot = RaffleBot(pin_result=False)
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    controller._group_admin = AsyncMock(return_value=(7, SimpleNamespace(), -100))  # type: ignore[method-assign]
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-100, type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=7),
        text="/lottery 30m 1 参与 GiftPass",
        answer=AsyncMock(),
    )

    await controller.lottery(message)  # type: ignore[arg-type]

    assert await storage.active_raffle(-100) is None
    assert await storage.pending_raffles() == ()
    assert bot.deleted == [(-100, 101)]
    message.answer.assert_awaited_once_with("抽奖发布失败，已安全取消。")


async def test_raffle_keyword_join_is_exact_deletes_source_and_confirms(
    tmp_path: Path, storage: Storage
) -> None:
    bot = RaffleBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    raffle = await storage.create_raffle(
        chat_id=-100,
        creator_id=7,
        keyword="Nexa",
        prize="GiftPass",
        winner_count=1,
        ends_at="2099-01-01T00:00:00+00:00",
    )
    assert await storage.set_raffle_message(raffle.id, 88)
    assert await storage.activate_raffle(raffle.id)
    raffle = await controller.raffle_service.runtime.sync(-100)
    assert raffle is not None
    active_lookup = AsyncMock(wraps=storage.active_raffle)
    storage.active_raffle = active_lookup  # type: ignore[method-assign]
    exact = group_message(42, "  ＮＥＸＡ\u200b  ", message_id=5)

    matched = await controller.raffle_service.keyword_filter(exact)
    assert isinstance(matched, dict)
    assert not await controller.raffle_service.keyword_filter(
        group_message(42, "Demo 参加", message_id=6)
    )
    await controller.raffle_service.join(exact, matched["raffle"])
    await controller.raffle_service.flush_card_updates()

    assert await storage.raffle_entry_ids(raffle.id) == (42,)
    assert bot.deleted == [(-100, 5)]
    assert bot.sent[-1] == (
        -100,
        '<a href="tg://user?id=42">Member</a>，已成功参与抽奖！🎉\n当前共有 1 人参与。',
    )
    assert bot.edited[-1][1] == 88
    assert bot.member_checks == []
    active_lookup.assert_not_awaited()

    duplicate = group_message(42, "nexa", message_id=7)
    duplicate_match = await controller.raffle_service.keyword_filter(duplicate)
    assert isinstance(duplicate_match, dict)
    await controller.raffle_service.join(duplicate, duplicate_match["raffle"])
    assert await storage.raffle_entry_ids(raffle.id) == (42,)
    assert bot.deleted[-1] == (-100, 7)
    assert bot.sent[-1] == (
        -100,
        '<a href="tg://user?id=42">Member</a>，你已经参与过啦，本次不会重复登记。',
    )


async def test_ordinary_short_message_does_not_query_raffle_storage(
    tmp_path: Path, storage: Storage
) -> None:
    controller = ModerationController(RaffleBot(), settings(tmp_path), storage)  # type: ignore[arg-type]
    active_lookup = AsyncMock(wraps=storage.active_raffle)
    storage.active_raffle = active_lookup  # type: ignore[method-assign]

    matched = await controller.raffle_service.keyword_filter(group_message(42, "正常聊天"))

    assert matched is False
    active_lookup.assert_not_awaited()


async def test_five_hundred_raffle_joins_coalesce_card_edits(
    tmp_path: Path, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(moderator_module, "RAFFLE_CARD_DEBOUNCE_SECONDS", 60)
    bot = RaffleBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    created = await storage.create_raffle(
        chat_id=-100,
        creator_id=7,
        keyword="参与",
        prize="GiftPass",
        winner_count=1,
        ends_at="2099-01-01T00:00:00+00:00",
    )
    assert await storage.set_raffle_message(created.id, 88)
    assert await storage.activate_raffle(created.id)
    raffle = await controller.raffle_service.runtime.sync(-100)
    assert raffle is not None

    for user_id in range(1_000, 1_500):
        await controller.raffle_service.join(
            group_message(user_id, "参与", message_id=user_id),
            raffle,
        )
    await controller.raffle_service.flush_card_updates()

    assert await storage.raffle_entry_count(raffle.id) == 500
    assert len(bot.edited) == 1
    assert "参与人数：500" in bot.edited[0][2]
    assert bot.member_checks == []


def test_raffle_participant_mention_prefers_public_username() -> None:
    user = User(id=42, is_bot=False, first_name="Member", username="example_member")

    assert RaffleService.participant_mention(user, 42) == "@example_member"


async def test_unique_authorized_anonymous_admin_can_join_keyword_raffle(
    tmp_path: Path, storage: Storage
) -> None:
    bot = RaffleBot(anonymous_admin=True)
    controller = ModerationController(
        bot,
        settings(tmp_path, authorized_admin_ids=frozenset({7})),  # type: ignore[arg-type]
        storage,
    )
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
    raffle = await controller.raffle_service.runtime.sync(-100)
    assert raffle is not None
    anonymous = Message(
        message_id=9,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        from_user=User(id=1087968824, is_bot=True, first_name="GroupAnonymousBot"),
        sender_chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        text="参与",
    )

    matched = await controller.raffle_service.keyword_filter(anonymous)
    assert isinstance(matched, dict)
    await controller.raffle_service.join(anonymous, matched["raffle"])
    await controller.raffle_service.flush_card_updates()

    assert await storage.raffle_entry_ids(raffle.id) == (7,)
    assert bot.deleted == [(-100, 9)]
    assert bot.sent[-1] == (
        -100,
        '<a href="tg://user?id=7">用户</a>，已成功参与抽奖！🎉\n当前共有 1 人参与。',
    )


@pytest.mark.parametrize("operation", ("end", "cancel"))
async def test_raffle_end_and_cancel_unpin_exact_card(
    tmp_path: Path, storage: Storage, operation: str
) -> None:
    bot = RaffleBot()
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
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
    raffle = await controller.raffle_service.runtime.sync(-100)
    assert raffle is not None

    if operation == "end":
        await controller.raffle_service.finalize(raffle, notify_admin=False)
    else:
        await controller.raffle_service.cancel_active(-100)

    assert bot.unpinned == [(-100, 88)]
    assert bot.edited[-1][1] == 88
    assert await storage.active_raffle(-100) is None
    queued = await storage.due_ephemeral("2100-01-01T00:00:00+00:00")
    assert {(item.chat_id, item.message_id) for item in queued} == {(-100, 88)}


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


async def test_manual_ban_ignores_legacy_reason_text(tmp_path: Path, storage: Storage) -> None:
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
    await controller.ban(ban_message)  # type: ignore[arg-type]

    assert bot.banned == [(-100, 42, True)]
    assert bot.unbanned == []
    ban_message.answer.assert_awaited_once_with("已封禁 <code>42</code>。")
    rows = await storage.db.execute_fetchall(
        "SELECT reason FROM moderation_log ORDER BY id",
    )
    assert [row["reason"] for row in rows] == [""]


def test_stats_cutoffs_use_beijing_calendar_days() -> None:
    now = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
    today, week = stats_cutoffs(now)
    assert today == "2026-08-26T16:00:00+00:00"
    assert week == "2026-08-20T16:00:00+00:00"


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
    await controller.reporter.start()
    for _ in range(50):
        if bot.sent:
            break
        await asyncio.sleep(0.001)
    await controller.reporter.shutdown()

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

    private_guard = controller.router.message.handlers[0]
    member_command_guard = controller.router.message.handlers[3]
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
    await seed_trusted_user(storage, -100, 43, 42)

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
    assert await warning_count(storage, -100, 42) == 1


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
    await seed_trusted_user(storage, -100, 43, 42)

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
    assert await warning_count(storage, -100, 42) == 1
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
    assert await warning_count(storage, -100, 42) == 0
    candidates = await storage.ai_signatures(-100, statuses=("candidate",))
    assert [(item.display_signature, item.hit_count) for item in candidates] == [
        ("免费领取 usdt", 2)
    ]
    rejected = await storage.ai_signatures(-100, statuses=("rejected",))
    assert [(item.display_signature, item.hit_count) for item in rejected] == [("合作", 2)]
    decisions = await recent_ai_decisions(storage, -100, limit=2)
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
    decisions = await recent_ai_decisions(storage, -100, limit=1)
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
    assert await warning_count(storage, -100, 42) == 0
    assert (await storage.chat_stats(-100)).warned == 0
    assert (await storage.ai_stats(-100)).failed == 1
    controller._notice.assert_awaited_once_with(ANY, "审核服务异常，消息已删除。")


async def test_moderation_policy_cache_invalidates_on_runtime_change(
    tmp_path: Path, storage: Storage
) -> None:
    controller = ModerationController(  # type: ignore[arg-type]
        FakeBot(),
        settings(tmp_path),
        storage,
    )
    await seed_allowed_domain(storage, -100, "example.com", 7)
    await storage.upsert_ai_signature(
        chat_id=-100,
        normalized_signature="免费领取 usdt",
        display_signature="免费领取 USDT",
        category="promotion",
        status="active",
        confidence=0.99,
    )
    policy = await controller._moderation_policy(-100)
    cached = await controller._moderation_policy(-100)

    assert policy is cached
    assert policy.allowed_domains == frozenset({"example.com"})
    assert policy.active_signatures == frozenset({"免费领取 usdt"})
    assert not policy.paused

    await storage.set_runtime_setting(-100, "ai_paused", "1")
    controller._invalidate_policy(-100)
    assert (await controller._moderation_policy(-100)).paused


async def test_policy_invalidation_during_load_retries_with_fresh_state(
    tmp_path: Path, storage: Storage
) -> None:
    controller = ModerationController(  # type: ignore[arg-type]
        FakeBot(),
        settings(tmp_path),
        storage,
    )
    started = asyncio.Event()
    resume = asyncio.Event()
    original_runtime_setting = storage.runtime_setting
    calls = 0

    async def delayed_runtime_setting(chat_id: int, key: str) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await resume.wait()
        return await original_runtime_setting(chat_id, key)

    storage.runtime_setting = delayed_runtime_setting  # type: ignore[method-assign]
    loading = asyncio.create_task(controller._moderation_policy(-100))
    await started.wait()
    await storage.set_runtime_setting(-100, "ai_paused", "1")
    controller._invalidate_policy(-100)
    resume.set()

    assert (await loading).paused
    assert calls == 2


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
        video=SimpleNamespace(cover=None, thumbnail=thumbnail, file_size=50_000_000),
    )

    result = await controller._message_image_data_url(message)  # type: ignore[arg-type]

    assert result == "data:image/jpeg;base64,dGh1bWJuYWls"
    bot.download.assert_awaited_once_with(thumbnail, destination=ANY)  # type: ignore[attr-defined]


async def test_video_prefers_sized_cover_over_legacy_thumbnail(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot()

    async def download(file: object, *, destination: object) -> None:
        assert file is target_cover
        destination.write(b"cover")

    small_cover = SimpleNamespace(file_size=5, width=640, height=360)
    target_cover = SimpleNamespace(file_size=10, width=1280, height=720)
    large_cover = SimpleNamespace(file_size=20, width=1920, height=1080)
    thumbnail = SimpleNamespace(file_size=9)
    bot.download = AsyncMock(side_effect=download)  # type: ignore[attr-defined]
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    message = SimpleNamespace(
        photo=None,
        video=SimpleNamespace(
            cover=[small_cover, target_cover, large_cover],
            thumbnail=thumbnail,
            file_size=50_000_000,
        ),
    )

    result = await controller._message_image_data_url(message)  # type: ignore[arg-type]

    assert result == "data:image/jpeg;base64,Y292ZXI="
    bot.download.assert_awaited_once_with(target_cover, destination=ANY)  # type: ignore[attr-defined]


async def test_cover_only_video_is_reviewed_once_without_thinking(
    tmp_path: Path, storage: Storage
) -> None:
    bot = FakeBot()

    async def download(_file: object, *, destination: object) -> None:
        destination.write(b"cover")

    bot.download = AsyncMock(side_effect=download)  # type: ignore[attr-defined]
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad=False,
                confidence=0.99,
                category="normal",
                signatures=(),
                cached=False,
                latency_ms=10,
                reasoning_effort="none",
            )
        )
    )
    controller = ModerationController(
        bot,
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
        video=Video(
            file_id="video",
            file_unique_id="video-unique",
            width=1280,
            height=720,
            duration=5,
            cover=[
                PhotoSize(
                    file_id="cover",
                    file_unique_id="cover-unique",
                    width=1280,
                    height=720,
                    file_size=10,
                )
            ],
        ),
    )

    await controller.moderate_message(message)

    assert reviewer.review.await_args.kwargs["image_data_url"] == (
        "data:image/jpeg;base64,Y292ZXI="
    )
    assert reviewer.review.await_count == 1
    assert reviewer.review.await_args.kwargs["reasoning_effort"] == "none"
    controller._processing_notice.assert_not_awaited()


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
    assert await warning_count(storage, -100, 42) == 0
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
    assert await warning_count(storage, -100, 42) == 0
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
    assert await warning_count(storage, -100, 42) == 0
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
    assert await warning_count(storage, -100, 42) == 0
    assert await storage.active_ai_signatures(-100) == {"免费领取 usdt"}
    decisions = await recent_ai_decisions(storage, -100, limit=1)
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
    assert await warning_count(storage, -100, 42) == 0
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
    assert await warning_count(storage, -100, 42) == 0
    decision = (await recent_ai_decisions(storage, -100, limit=1))[0]
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
    assert await recent_ai_decisions(storage, -100, limit=1) == ()


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
    decision = (await recent_ai_decisions(storage, -100, limit=1))[0]
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


async def test_normal_text_uses_no_thinking_without_a_visible_notice(
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
                latency_ms=20,
                reasoning_effort="none",
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(
            tmp_path,
            ai_enabled=True,
            ai_normal_reasoning_effort="none",
            ai_suspicious_reasoning_effort="none",
        ),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)

    await controller.moderate_message(group_message(42, "正常咨询"))

    assert reviewer.review.await_count == 1
    assert reviewer.review.await_args.kwargs["reasoning_effort"] == "none"
    controller._processing_notice.assert_not_awaited()
    assert bot.deleted == []


@pytest.mark.parametrize(
    ("is_ad", "confidence", "expected_deleted"),
    ((False, 0.70, False), (True, 0.70, False), (True, 0.99, True)),
)
async def test_non_thinking_decision_never_triggers_a_second_review(
    tmp_path: Path,
    storage: Storage,
    is_ad: bool,
    confidence: float,
    expected_deleted: bool,
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                is_ad,
                confidence,
                "promotion" if is_ad else "normal",
                (),
                False,
                15,
                reasoning_effort="none",
                attempt_count=1,
            )
        )
    )
    bot = FakeBot()
    controller = ModerationController(
        bot,
        settings(
            tmp_path,
            ai_enabled=True,
            ai_normal_reasoning_effort="none",
            ai_suspicious_reasoning_effort="none",
        ),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )

    controller._notice = AsyncMock()
    await controller.moderate_message(group_message(42, "帮我看看价格"))

    assert reviewer.review.await_count == 1
    assert reviewer.review.await_args.kwargs["reasoning_effort"] == "none"
    assert bool(bot.deleted) is expected_deleted
    decision = (await recent_ai_decisions(storage, -100, limit=1))[0]
    assert decision.review_path == "gateway"
    assert decision.latency_ms == 15
    assert decision.attempt_count == 1


async def test_suspicious_text_uses_no_thinking_and_a_visible_notice(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                False,
                0.99,
                "normal",
                (),
                False,
                20,
                reasoning_effort="none",
            )
        )
    )
    controller = ModerationController(
        FakeBot(),
        settings(
            tmp_path,
            ai_enabled=True,
            ai_normal_reasoning_effort="none",
            ai_suspicious_reasoning_effort="none",
        ),  # type: ignore[arg-type]
        storage,
        ai_reviewer=reviewer,  # type: ignore[arg-type]
    )
    controller._processing_notice = AsyncMock(return_value=None)

    await controller.moderate_message(group_message(42, "https://external.example"))

    assert reviewer.review.await_count == 1
    assert reviewer.review.await_args.kwargs["reasoning_effort"] == "none"
    controller._processing_notice.assert_awaited_once()


async def test_already_deleted_ad_still_receives_warning(tmp_path: Path, storage: Storage) -> None:
    bot = FakeBot()
    bot.delete_message = AsyncMock(  # type: ignore[method-assign]
        side_effect=TelegramBadRequest(
            method=DeleteMessage(chat_id=-100, message_id=1),
            message="message to delete not found",
        )
    )
    controller = ModerationController(bot, settings(tmp_path), storage)  # type: ignore[arg-type]
    controller._notice = AsyncMock()

    enforced = await controller._handle_violation(
        group_message(42, "广告"),
        42,
        Violation("test", ""),
    )

    assert enforced is True
    assert await warning_count(storage, -100, 42) == 1
    stats = await storage.chat_stats(-100)
    assert stats.deleted == 1
    assert stats.warned == 1


async def test_learning_write_failure_does_not_undo_ad_enforcement(
    tmp_path: Path, storage: Storage
) -> None:
    reviewer = SimpleNamespace(
        review=AsyncMock(
            return_value=AIReviewResult(
                True,
                0.99,
                "promotion",
                ("免费领取 usdt",),
                False,
                20,
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
    storage.record_ai_learning = AsyncMock(side_effect=RuntimeError("database unavailable"))  # type: ignore[method-assign]
    controller._processing_notice = AsyncMock(return_value=None)
    controller._notice = AsyncMock()

    await controller.moderate_message(group_message(42, "免费领取 USDT"))

    assert bot.deleted == [(-100, 1)]
    assert await warning_count(storage, -100, 42) == 1


def test_photo_preview_uses_smallest_safe_size_at_target() -> None:
    photos = [
        PhotoSize(file_id="a", file_unique_id="a", width=320, height=320, file_size=10),
        PhotoSize(file_id="b", file_unique_id="b", width=1280, height=720, file_size=100),
        PhotoSize(file_id="c", file_unique_id="c", width=1920, height=1080, file_size=200),
    ]

    selected = select_photo_preview(photos, target_long_edge=1280, maximum_bytes=150)

    assert selected is photos[1]
