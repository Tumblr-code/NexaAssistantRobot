from __future__ import annotations

import asyncio
import base64
import html
import io
import logging
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message, PhotoSize, User
from aiohttp import ClientError as AiohttpClientError

from nexa_guardian.admin import AdminRights, AdminService
from nexa_guardian.ai_review import (
    AIReviewer,
    AIReviewError,
    suspicious_precheck,
)
from nexa_guardian.command_ui import COMMAND_SPECS, bot_commands, help_text
from nexa_guardian.commands import command_arguments, resolve_target
from nexa_guardian.config import Settings
from nexa_guardian.detection import (
    AdDetector,
    DetectionInput,
    FloodGuard,
    Violation,
    domain_from_url,
    extract_entity_urls,
)
from nexa_guardian.history_purge import (
    HistoryPurgeError,
    HistoryPurger,
    TelegramAdminSession,
)
from nexa_guardian.member_cleanup import MemberScanner
from nexa_guardian.member_cleanup_service import (
    MemberActivityMiddleware,
    MemberCleanupService,
)
from nexa_guardian.raffle_service import RaffleService
from nexa_guardian.reporting import ActionReporter
from nexa_guardian.status_service import StatusService
from nexa_guardian.storage import Storage

logger = logging.getLogger(__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
AUTO_BAN_MEMORY_SECONDS = 60 * 60
AUTO_BAN_MEMORY_MAX = 10_000
AI_CANDIDATE_PROMOTION_HITS = 3
RAFFLE_WORKER_INTERVAL_SECONDS = 2
RAFFLE_CARD_DEBOUNCE_SECONDS = 2
MODERATION_POLICY_CACHE_SECONDS = 5
TRUST_CACHE_SECONDS = 60
TRUST_CACHE_MAX = 4096
BOT_PROFILE_NAME = "Nexa 群管助手"
BOT_PROFILE_SHORT_DESCRIPTION = (
    "Nexa 官方群管理审核员：GPT-6 Astra 智能识别广告与刷屏，自动删除并警告；"
    "第 3 次直接封禁，并支持安全透明的群抽奖。"
)
BOT_PROFILE_DESCRIPTION = """Nexa Guardian 是 @NexaStoreGroup 的官方群管理与安全审核员。

• GPT-6 Astra 审核文字、图片及视频封面
• 自动清理广告、诈骗、恶意推广与刷屏；第 3 次警告直接封禁
• 普通聊天静默处理；仅可疑消息显示审核状态
• 持续学习高置信广告特征，提升后续拦截效率
• 极简管理入口：状态、删除、封禁与群抽奖
• 私聊支持状态汇总、群历史清理及长期不活跃成员安全预览
• 抽奖支持关键词报名、自动置顶、定时开奖、取消及重抽
• 群内机器人回复约 30 秒后清理；管理员私聊回复永久保留
• 不保存群消息原文、图片或模型推理

发送 /help 查看管理命令。

官方服务：
Nexa Store 商城：https://nexastore.cn
Nexa · ModelPass：https://openai.nexastore.cn
Nexa · GiftPass：https://premium.nexastore.cn"""
AI_LEARNING_EVIDENCE = (
    "link",
    "mention",
    "forward",
    "contact",
    "obfuscation",
    "promotion",
    "flood",
    "duplicate",
    "text",
    "image",
)


class MediaPreviewTooLarge(ValueError):
    """Raised before a Telegram preview can exceed the configured memory bound."""


class BoundedBytesIO(io.BytesIO):
    def __init__(self, maximum_bytes: int) -> None:
        super().__init__()
        self.maximum_bytes = maximum_bytes

    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self.maximum_bytes:
            raise MediaPreviewTooLarge("media preview exceeded configured byte limit")
        return super().write(data)


def select_photo_preview(
    photos: list[PhotoSize],
    *,
    target_long_edge: int,
    maximum_bytes: int,
) -> PhotoSize | None:
    candidates = [
        photo
        for photo in photos
        if (file_size := getattr(photo, "file_size", None)) is None
        or int(file_size) <= maximum_bytes
    ]
    if not candidates:
        return None

    def long_edge(photo: PhotoSize) -> int:
        return max(int(getattr(photo, "width", 0)), int(getattr(photo, "height", 0)))

    at_target = [photo for photo in candidates if long_edge(photo) >= target_long_edge]
    if at_target:
        return min(at_target, key=long_edge)
    return max(candidates, key=long_edge)


@dataclass(frozen=True, slots=True)
class ModerationPolicySnapshot:
    expires_at: float
    paused: bool
    active_signatures: frozenset[str]
    allowed_domains: frozenset[str]


class AIDecisionMetrics(TypedDict):
    reasoning_effort: str
    service_tier: str
    review_path: str
    media_latency_ms: int
    queue_latency_ms: int
    request_latency_ms: int
    action_latency_ms: int
    total_latency_ms: int
    attempt_count: int


HELP_TEXT = help_text()


def display_user(user: User | None, user_id: int) -> str:
    if user is None:
        return f"<code>{user_id}</code>"
    name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def telegram_message_is_absent(error: TelegramBadRequest) -> bool:
    return "message to delete not found" in str(error).casefold()


class ModerationController:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        storage: Storage,
        *,
        ai_reviewer: AIReviewer | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.storage = storage
        self.router = Router(name="nexa_guardian")
        self.admins = AdminService(bot)
        self.reporter = ActionReporter(bot, storage, settings.log_chat_id)
        self.detector = AdDetector(
            block_external_links=settings.block_external_links,
            block_forwards=settings.block_forwards,
            block_channel_mentions=settings.block_channel_mentions,
        )
        self.flood = FloodGuard(
            window_seconds=settings.flood_window_seconds,
            max_messages=settings.flood_max_messages,
            duplicate_window_seconds=settings.duplicate_window_seconds,
            duplicate_max_messages=settings.duplicate_max_messages,
        )
        self.ai_reviewer = ai_reviewer
        if self.ai_reviewer is None and settings.ai_enabled:
            self.ai_reviewer = AIReviewer(
                base_url=settings.openai_base_url,
                api_key=settings.openai_api_key,
                model=settings.ai_model,
                reasoning_effort=settings.ai_reasoning_effort,
                service_tier=settings.ai_service_tier,
                timeout=settings.ai_timeout_seconds,
                concurrency=settings.ai_concurrency,
                cache_ttl=settings.ai_cache_ttl_seconds,
                cache_size=settings.ai_cache_max_entries,
            )
        self._ai_failure_streak = 0
        self._ai_failure_lock = asyncio.Lock()
        self._media_semaphore = asyncio.Semaphore(settings.ai_media_concurrency)
        self._member_action_locks: weakref.WeakValueDictionary[tuple[int, int], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._auto_banned_members: OrderedDict[tuple[int, int], float] = OrderedDict()
        self._policy_cache: dict[int, ModerationPolicySnapshot] = {}
        self._policy_locks: dict[int, asyncio.Lock] = {}
        self._policy_versions: dict[int, int] = {}
        self._trusted_cache: OrderedDict[tuple[int, int], tuple[float, bool]] = OrderedDict()
        self.raffle_service = RaffleService(
            bot,
            settings,
            storage,
            self.admins,
            worker_interval_seconds=RAFFLE_WORKER_INTERVAL_SECONDS,
            card_debounce_seconds=RAFFLE_CARD_DEBOUNCE_SECONDS,
        )
        telegram_admin_session = TelegramAdminSession(
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            session_path=settings.telegram_session_path,
            authorized_user_ids=settings.authorized_admin_ids,
            allowed_chat_ids=settings.allowed_chat_ids,
        )
        self.telegram_admin_session = telegram_admin_session
        self.status_service = StatusService(
            bot,
            settings,
            storage,
            telegram_admin_session,
        )
        self.history_purger = HistoryPurger(
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            session_path=settings.telegram_session_path,
            authorized_user_ids=settings.authorized_admin_ids,
            allowed_chat_ids=settings.allowed_chat_ids,
            session=telegram_admin_session,
        )
        self.member_scanner = MemberScanner(
            telegram_admin_session,
            authorized_user_ids=settings.authorized_admin_ids,
        )
        self.member_cleanup = MemberCleanupService(
            bot,
            settings,
            storage,
            self.member_scanner,
            self.reporter,
            finish_processing=self._finish_processing,
            member_lock=self._member_action_lock,
            active_raffle_participants=self.raffle_service.active_participant_ids,
        )
        self.member_activity_tracker = self.member_cleanup.activity
        self._register_handlers()

    def _register_handlers(self) -> None:
        activity_middleware = MemberActivityMiddleware(
            self.member_activity_tracker,
            allowed_chat_ids=self.settings.allowed_chat_ids,
        )
        self.router.message.outer_middleware(activity_middleware)
        self.router.edited_message.outer_middleware(activity_middleware)
        # Private updates from anyone except the single configured administrator
        # are silently consumed before any command handler can respond.
        self.router.message.register(
            self._ignore_unauthorized_private,
            self._unauthorized_private_message,
        )
        self.router.message.register(
            self.raffle_service.delete_pin_service_message,
            self.raffle_service.pin_service_filter,
        )
        self.router.message.register(
            self.raffle_service.join,
            self.raffle_service.keyword_filter,
        )
        # Member-authored command-looking messages are still untrusted content.
        # Register this guard before Telegram command handlers so only verified
        # admins (and other moderation exemptions) can reach the command path.
        self.router.message.register(
            self.moderate_message,
            self._ordinary_member_command,
        )
        command_handlers = {
            "help": self.help,
            "status": self.status,
            "ban": self.ban,
            "del": self.delete_replied,
            "lottery": self.lottery,
        }
        for spec in COMMAND_SPECS:
            self.router.message.register(command_handlers[spec.name], Command(spec.name))
        self.router.message.register(self.moderate_message)
        self.router.edited_message.register(self.moderate_message)
        self.router.chat_member.register(self.chat_member_changed)

    async def _unauthorized_private_message(self, message: Message) -> bool:
        if message.chat.type != ChatType.PRIVATE:
            return False
        if message.from_user is None or len(self.settings.authorized_admin_ids) != 1:
            return True
        authorized_admin_id = next(iter(self.settings.authorized_admin_ids))
        return message.from_user.id != authorized_admin_id

    async def _ignore_unauthorized_private(self, message: Message) -> None:
        """Silently consume unauthorized private updates."""
        return None

    async def _ordinary_member_command(self, message: Message) -> bool:
        """Route untrusted group command-shaped text through moderation first."""
        if not message.text or not message.text.startswith("/"):
            return False
        if message.chat.type not in GROUP_TYPES or not self.is_allowed_chat(message.chat.id):
            return False
        if message.is_automatic_forward:
            return False
        if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
            return False

        user = message.from_user
        user_id = user.id if user is not None and not user.is_bot else None
        if user_id is not None:
            admin_state = await self.admins.cached_is_admin(message.chat.id, user_id)
            if admin_state is not False:
                return False
            if await self._is_trusted(message.chat.id, user_id):
                return False
            return True
        return message.sender_chat is not None

    async def set_commands(self) -> None:
        commands = bot_commands()
        for language_code in (None, "zh"):
            await self._sync_bot_value(
                label="commands",
                language_code=language_code,
                expected=tuple((item.command, item.description) for item in commands),
                read=lambda code=language_code: self.bot.get_my_commands(language_code=code),
                current_value=lambda items: tuple(
                    (item.command, item.description) for item in items
                ),
                write=lambda code=language_code: self.bot.set_my_commands(
                    commands, language_code=code
                ),
            )
            await self._sync_bot_value(
                label="name",
                language_code=language_code,
                expected=BOT_PROFILE_NAME,
                read=lambda code=language_code: self.bot.get_my_name(language_code=code),
                current_value=lambda item: item.name,
                write=lambda code=language_code: self.bot.set_my_name(
                    BOT_PROFILE_NAME, language_code=code
                ),
            )
            await self._sync_bot_value(
                label="short_description",
                language_code=language_code,
                expected=BOT_PROFILE_SHORT_DESCRIPTION,
                read=lambda code=language_code: self.bot.get_my_short_description(
                    language_code=code
                ),
                current_value=lambda item: item.short_description,
                write=lambda code=language_code: self.bot.set_my_short_description(
                    BOT_PROFILE_SHORT_DESCRIPTION, language_code=code
                ),
            )
            await self._sync_bot_value(
                label="description",
                language_code=language_code,
                expected=BOT_PROFILE_DESCRIPTION,
                read=lambda code=language_code: self.bot.get_my_description(language_code=code),
                current_value=lambda item: item.description,
                write=lambda code=language_code: self.bot.set_my_description(
                    BOT_PROFILE_DESCRIPTION, language_code=code
                ),
            )

    async def _sync_bot_value(
        self,
        *,
        label: str,
        language_code: str | None,
        expected: object,
        read: Callable[[], Awaitable[Any]],
        current_value: Callable[[Any], object],
        write: Callable[[], Awaitable[Any]],
    ) -> None:
        language = language_code or "default"
        try:
            current = current_value(await read())
        except TelegramAPIError:
            logger.exception("读取机器人资料失败 field=%s language=%s", label, language)
            return
        if current == expected:
            return
        try:
            await write()
        except TelegramRetryAfter as exc:
            logger.warning(
                "机器人资料同步受限 field=%s language=%s retry_after=%s",
                label,
                language,
                exc.retry_after,
            )
        except TelegramAPIError:
            logger.exception("同步机器人资料失败 field=%s language=%s", label, language)

    async def startup(self) -> None:
        await self.reporter.start()
        await self.raffle_service.startup()

    async def shutdown(self) -> None:
        await self.raffle_service.shutdown()
        await self.reporter.shutdown()
        close_reviewer = getattr(self.ai_reviewer, "close", None)
        if close_reviewer is not None:
            await close_reviewer()

    async def _moderation_policy(self, chat_id: int) -> ModerationPolicySnapshot:
        now = time.monotonic()
        cached = self._policy_cache.get(chat_id)
        if cached is not None and cached.expires_at > now:
            return cached
        lock = self._policy_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            while True:
                now = time.monotonic()
                cached = self._policy_cache.get(chat_id)
                if cached is not None and cached.expires_at > now:
                    return cached
                version = self._policy_versions.get(chat_id, 0)
                paused, signatures, domains = await asyncio.gather(
                    self.storage.runtime_setting(chat_id, "ai_paused"),
                    self.storage.active_ai_signatures(chat_id),
                    self.storage.allowed_domains(chat_id),
                )
                if self._policy_versions.get(chat_id, 0) != version:
                    continue
                snapshot = ModerationPolicySnapshot(
                    expires_at=now + MODERATION_POLICY_CACHE_SECONDS,
                    paused=paused == "1",
                    active_signatures=frozenset(signatures),
                    allowed_domains=frozenset(domains),
                )
                self._policy_cache[chat_id] = snapshot
                return snapshot

    def _invalidate_policy(self, chat_id: int) -> None:
        self._policy_cache.pop(chat_id, None)
        self._policy_versions[chat_id] = self._policy_versions.get(chat_id, 0) + 1

    async def _is_trusted(self, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        now = time.monotonic()
        cached = self._trusted_cache.pop(key, None)
        if cached is not None and cached[0] > now:
            self._trusted_cache[key] = cached
            return cached[1]
        result = await self.storage.is_trusted(chat_id, user_id)
        self._trusted_cache[key] = (now + TRUST_CACHE_SECONDS, result)
        while len(self._trusted_cache) > TRUST_CACHE_MAX:
            self._trusted_cache.popitem(last=False)
        return result

    def is_allowed_chat(self, chat_id: int) -> bool:
        return chat_id in self.settings.allowed_chat_ids

    async def _group_admin(
        self,
        message: Message,
        *,
        require_allowed: bool = True,
        restrict: bool = False,
        delete: bool = False,
    ) -> tuple[int, AdminRights, int] | None:
        if message.from_user is None:
            await message.answer("无法识别命令发起者。")
            return None
        if message.chat.type == ChatType.PRIVATE:
            if len(self.settings.authorized_admin_ids) != 1:
                await message.answer("私聊管理要求恰好配置一位授权管理员。")
                return None
            authorized_admin_id = next(iter(self.settings.authorized_admin_ids))
            if message.from_user.id != authorized_admin_id:
                await message.answer("你不在本机器人的授权管理员名单中。")
                return None
            if len(self.settings.allowed_chat_ids) != 1:
                await message.answer("私聊管理要求当前恰好配置一个授权群。")
                return None
            target_chat_id = next(iter(self.settings.allowed_chat_ids))
            try:
                rights = await self.admins.rights(target_chat_id, message.from_user.id)
            except (TelegramAPIError, asyncio.TimeoutError):
                logger.exception(
                    "检查私聊管理员权限失败 chat_id=%s user_id=%s",
                    target_chat_id,
                    message.from_user.id,
                )
                await message.answer("暂时无法向 Telegram 核验管理员权限，请稍后再试。")
                return None
            if not rights.is_admin:
                await message.answer("你已不再是目标群管理员，私聊管理已拒绝。")
                return None
            if restrict and not rights.can_restrict_members:
                await message.answer("你的管理员角色没有“封禁/限制成员”权限。")
                return None
            if delete and not rights.can_delete_messages:
                await message.answer("你的管理员角色没有“删除消息”权限。")
                return None
            return message.from_user.id, rights, target_chat_id
        if message.chat.type not in GROUP_TYPES:
            await message.answer("此命令只能由群管理员在群内或私聊机器人使用。")
            return None
        if require_allowed and not self.is_allowed_chat(message.chat.id):
            await message.answer(
                "此群尚未加入 <code>ALLOWED_CHAT_IDS</code>，请先在服务器配置目标群 ID。"
            )
            return None
        if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
            try:
                anonymous_actor = await self.admins.unique_anonymous_actor(
                    message.chat.id, self.settings.authorized_admin_ids
                )
            except TelegramAPIError:
                logger.exception("检查匿名管理员失败 chat_id=%s", message.chat.id)
                await message.answer("暂时无法向 Telegram 核验匿名管理员，请稍后再试。")
                return None
            if anonymous_actor is None:
                await message.answer(
                    "无法唯一核验匿名管理员。请关闭“保持匿名”后重试，或检查授权名单。"
                )
                return None
            actor_id, rights = anonymous_actor
            if restrict and not rights.can_restrict_members:
                await message.answer("你的管理员角色没有“封禁/限制成员”权限。")
                return None
            if delete and not rights.can_delete_messages:
                await message.answer("你的管理员角色没有“删除消息”权限。")
                return None
            return actor_id, rights, message.chat.id
        if (
            self.settings.authorized_admin_ids
            and message.from_user.id not in self.settings.authorized_admin_ids
        ):
            await message.answer("你不在本机器人的授权管理员名单中。")
            return None
        try:
            rights = await self.admins.rights(message.chat.id, message.from_user.id)
        except (TelegramAPIError, asyncio.TimeoutError):
            logger.exception("检查管理员权限失败 chat_id=%s", message.chat.id)
            await message.answer("暂时无法向 Telegram 核验管理员权限，请稍后再试。")
            return None
        if not rights.is_admin:
            await message.answer("只有本群管理员可以执行此命令。")
            return None
        if restrict and not rights.can_restrict_members:
            await message.answer("你的管理员角色没有“封禁/限制成员”权限。")
            return None
        if delete and not rights.can_delete_messages:
            await message.answer("你的管理员角色没有“删除消息”权限。")
            return None
        return message.from_user.id, rights, message.chat.id

    async def _safe_target(
        self, message: Message, chat_id: int
    ) -> tuple[int, User | None, tuple[str, ...]] | None:
        try:
            target = resolve_target(message)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return None
        try:
            if await self.admins.target_is_admin(chat_id, target.user_id):
                await message.answer("不能对群主或管理员执行此操作。")
                return None
        except TelegramAPIError:
            logger.exception(
                "核验目标成员失败 chat_id=%s target_id=%s",
                chat_id,
                target.user_id,
            )
            await message.answer("无法核验目标成员身份，请确认用户 ID 或直接回复对方消息。")
            return None
        return target.user_id, target.user, target.arguments

    async def _notice(self, message: Message, text: str) -> None:
        try:
            await message.answer(text)
        except TelegramAPIError:
            logger.exception("发送群管提示失败 chat_id=%s", message.chat.id)

    async def _processing_notice(self, message: Message) -> Message | None:
        try:
            return await message.reply("正在审核…")
        except TelegramAPIError:
            logger.exception("发送审核提示失败 chat_id=%s", message.chat.id)
            return None

    async def _finish_processing(
        self, source: Message, processing: Message | None, text: str
    ) -> None:
        if processing is not None:
            try:
                await processing.edit_text(text)
                return
            except TelegramAPIError:
                logger.debug(
                    "编辑审核提示失败 chat_id=%s message_id=%s",
                    processing.chat.id,
                    processing.message_id,
                )
        await self._notice(source, text)

    async def help(self, message: Message) -> None:
        if await self._group_admin(message) is not None:
            await message.answer(HELP_TEXT)

    async def status(self, message: Message) -> None:
        actor = await self._group_admin(message, require_allowed=False)
        if actor is None:
            return
        chat_id = actor[2]
        arguments = command_arguments(message)
        if arguments and arguments[0].casefold() == "ai":
            if message.chat.type != ChatType.PRIVATE:
                await message.answer("AI 管理仅允许授权管理员在机器人私聊中使用。")
                return
            await self._status_ai(message, actor[0], chat_id, arguments[1:])
            return
        if arguments:
            await message.answer(
                "用法：<code>/status</code>\nAI 管理请私聊使用：<code>/status ai</code>"
            )
            return
        await message.answer(
            await self.status_service.overview(
                chat_id,
                reviewer_ready=self.ai_reviewer is not None,
                ai_failure_streak=self._ai_failure_streak,
            )
        )

    async def _status_ai(
        self,
        message: Message,
        actor_id: int,
        chat_id: int,
        arguments: tuple[str, ...],
    ) -> None:
        if not arguments:
            await message.answer(
                await self.status_service.ai_detail(
                    chat_id,
                    reviewer_ready=self.ai_reviewer is not None,
                    ai_failure_streak=self._ai_failure_streak,
                )
            )
            return
        operation = arguments[0].casefold()
        if operation in {"pause", "resume"} and len(arguments) == 1:
            async with self.status_service.ai_control_lock(chat_id):
                paused = operation == "pause"
                if not paused and (not self.settings.ai_enabled or self.ai_reviewer is None):
                    await message.answer("AI 审核器未配置完成，无法恢复。")
                    return
                changed = await self.status_service.set_ai_paused(chat_id, paused)
                self._invalidate_policy(chat_id)
                if changed:
                    await self.reporter.record(
                        chat_id=chat_id,
                        action="ai_pause" if paused else "ai_resume",
                        actor_id=actor_id,
                    )
                await message.answer(
                    "AI 审核已暂停。暂停期间普通成员消息保留。" if paused else "AI 审核已恢复。"
                )
            return
        if operation == "words" and len(arguments) == 1:
            for page in await self.status_service.ai_word_pages(chat_id):
                await message.answer(page)
            return
        if operation == "del" and len(arguments) == 2 and arguments[1].isdigit():
            signature_id = int(arguments[1])
            disabled = await self.status_service.disable_ai_signature(chat_id, signature_id)
            if disabled:
                self._invalidate_policy(chat_id)
                await self.reporter.record(
                    chat_id=chat_id,
                    action="ai_signature_disabled",
                    actor_id=actor_id,
                    target_id=signature_id,
                )
                await message.answer(f"已停用 AI 特征 <code>{signature_id}</code>。")
            else:
                await message.answer("未找到可停用的 active 特征。")
            return
        await message.answer(
            "用法：\n"
            "<code>/status ai</code>\n"
            "<code>/status ai pause</code>｜<code>/status ai resume</code>\n"
            "<code>/status ai words</code>\n"
            "<code>/status ai del 特征ID</code>"
        )

    async def lottery(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        await self.raffle_service.command(
            message,
            actor_id=actor[0],
            chat_id=actor[2],
        )

    async def ban(self, message: Message) -> None:
        actor = await self._group_admin(message, restrict=True)
        if actor is None or (target := await self._safe_target(message, actor[2])) is None:
            return
        chat_id = actor[2]
        user_id, user, _ = target
        key = (chat_id, user_id)
        async with self._member_action_lock(chat_id, user_id):
            try:
                await self.bot.ban_chat_member(chat_id, user_id, revoke_messages=True)
            except TelegramAPIError as exc:
                await message.answer(f"封禁失败：{html.escape(str(exc))}")
                return
            self._remember_auto_banned(key)
        await self.reporter.record(
            chat_id=chat_id,
            action="ban",
            actor_id=actor[0],
            target_id=user_id,
        )
        await message.answer(f"已封禁 {display_user(user, user_id)}。")

    async def delete_replied(self, message: Message) -> None:
        arguments = command_arguments(message)
        if arguments and arguments[0].casefold() == "members":
            await self._delete_members(message, arguments[1:])
            return
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        chat_id = actor[2]
        if arguments and arguments[0].casefold() == "all":
            async with self.raffle_service.chat_lock(chat_id):
                active = await self.raffle_service.ensure_active(chat_id)
                if active is not None:
                    await message.answer(
                        "当前有进行中的抽奖，请先执行 <code>/lottery cancel</code>。"
                    )
                    return
                await message.answer("正在清理群历史，期间群聊保持开放。")
                try:
                    result = await asyncio.wait_for(
                        self.history_purger.purge(chat_id),
                        timeout=15 * 60,
                    )
                except asyncio.TimeoutError:
                    await message.answer("群历史清理超过 15 分钟，任务已停止。")
                    return
                except HistoryPurgeError as exc:
                    await message.answer(html.escape(str(exc)))
                    return
                await self.reporter.record(
                    chat_id=chat_id,
                    action="history_purge",
                    actor_id=actor[0],
                )
            await message.answer(
                f"群历史清理完成：删除 {result.deleted}，"
                f"跳过 {result.skipped}，失败 {result.failed}。"
            )
            return
        target_message = message.reply_to_message
        if target_message is not None and message.chat.type in GROUP_TYPES:
            target_message_id = target_message.message_id
            target_user_id = target_message.from_user.id if target_message.from_user else None
        else:
            try:
                target_message_id = int(arguments[0])
                if target_message_id <= 0:
                    raise ValueError
            except (IndexError, ValueError):
                await message.answer(
                    "群内请回复消息后使用 /del；私聊请使用 <code>/del 群消息ID</code>。"
                )
                return
            target_user_id = None
        try:
            await self.bot.delete_message(chat_id, target_message_id)
        except TelegramAPIError as exc:
            await message.answer(f"删除失败：{html.escape(str(exc))}")
            return
        await self.reporter.record(
            chat_id=chat_id,
            action="delete",
            message_id=target_message_id,
            actor_id=actor[0],
            target_id=target_user_id,
        )
        await message.answer(f"已删除群消息 <code>{target_message_id}</code>。")

    async def _private_member_admin(
        self,
        message: Message,
        *,
        restrict: bool,
    ) -> tuple[int, AdminRights, int] | None:
        actor = await self._group_admin(message, restrict=restrict)
        if actor is None:
            return None
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("为避免公开成员 ID，此成员清理命令仅可在机器人私聊中使用。")
            return None
        return actor

    async def _delete_members(self, message: Message, arguments: tuple[str, ...]) -> None:
        actor = await self._private_member_admin(message, restrict=True)
        if actor is None:
            return
        await self.member_cleanup.command(
            message,
            arguments,
            actor_id=actor[0],
            chat_id=actor[2],
        )

    async def moderate_message(self, message: Message) -> None:
        if message.chat.type not in GROUP_TYPES or not self.is_allowed_chat(message.chat.id):
            return
        if message.is_automatic_forward:
            return
        if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
            return
        user = message.from_user
        user_id = user.id if user is not None and not user.is_bot else None
        if user_id is not None:
            if user_id in self.settings.authorized_admin_ids:
                return
            admin_state = await self.admins.cached_is_admin(message.chat.id, user_id)
            if admin_state is not False:
                return
            if await self._is_trusted(message.chat.id, user_id):
                return
        elif message.sender_chat is None:
            return

        text = message.text or message.caption or ""
        entities = message.entities if message.text is not None else message.caption_entities
        entity_urls = extract_entity_urls(text, entities)

        if not self.settings.ai_enabled or self.ai_reviewer is None:
            await self._moderate_legacy(message, user_id, text, entity_urls)
            return
        policy = await self._moderation_policy(message.chat.id)
        if policy.paused:
            return

        suspicion = suspicious_precheck(
            text,
            entity_urls=entity_urls,
            is_forwarded=message.forward_origin is not None,
            active_signatures=set(policy.active_signatures),
            allowed_domains=set(self.settings.allowed_domains) | set(policy.allowed_domains),
        )
        flood_violation = (
            self.flood.check(message.chat.id, user_id, text)
            if user_id is not None and message.edit_date is None
            else None
        )
        target_id = user_id or (message.sender_chat.id if message.sender_chat else None)
        if suspicion.matched_signature is not None:
            processing = await self._processing_notice(message)
            if await self._finish_if_ai_paused(message, processing):
                return
            enforced = await self._handle_violation(
                message,
                user_id,
                Violation("ai_signature", ""),
                processing=processing,
            )
            if enforced:
                try:
                    await self.storage.record_ai_signature_hit(
                        message.chat.id,
                        suspicion.matched_signature,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "AI 特征命中计数写入失败 chat_id=%s",
                        message.chat.id,
                    )
            await self.storage.record_ai_decision(
                chat_id=message.chat.id,
                message_id=message.message_id,
                target_id=target_id,
                is_ad=True,
                confidence=1.0,
                category="learned_signature",
                model="local-signature",
                latency_ms=0,
                cached=True,
                enforced=enforced,
                reasoning_effort="none",
                service_tier="local",
                review_path="local_signature",
                request_latency_ms=0,
                total_latency_ms=0,
                attempt_count=0,
            )
            return

        has_media_preview = bool(message.photo) or (
            message.video is not None
            and (bool(getattr(message.video, "cover", None)) or message.video.thumbnail is not None)
        )
        if not text.strip() and not has_media_preview:
            return

        processing_task = (
            asyncio.create_task(
                self._processing_notice(message),
                name=f"nexa-review-notice-{message.chat.id}-{message.message_id}",
            )
            if suspicion.is_suspicious or flood_violation is not None
            else None
        )

        media_started = time.monotonic()
        image_data_url = await self._message_image_data_url(message)
        media_latency_ms = max(0, int((time.monotonic() - media_started) * 1000))
        if not text.strip() and image_data_url is None:
            processing = await processing_task if processing_task is not None else None
            if processing is not None:
                await self._finish_processing(message, processing, "审核完成。")
            return

        review_started = time.monotonic()
        review_deadline = review_started + self.settings.ai_timeout_seconds
        try:
            safety_target = str(target_id or "unknown")
            risk_signals = set(suspicion.reasons)
            if flood_violation is not None:
                risk_signals.add(flood_violation.code)
            normalized_risk_signals = tuple(
                signal for signal in AI_LEARNING_EVIDENCE if signal in risk_signals
            )
            linked_hosts = tuple(
                dict.fromkeys(
                    host
                    for raw_url in entity_urls
                    if (host := domain_from_url(raw_url)) is not None
                )
            )
            result = await asyncio.wait_for(
                self._review_message(
                    text=text,
                    image_data_url=image_data_url,
                    safety_identifier=f"{message.chat.id}:{safety_target}",
                    risk_signals=normalized_risk_signals,
                    linked_hosts=linked_hosts,
                    deadline=review_deadline,
                ),
                timeout=max(0.0, review_deadline - time.monotonic()),
            )
        except (AIReviewError, AiohttpClientError, asyncio.TimeoutError) as exc:
            processing = await processing_task if processing_task is not None else None
            await self._handle_ai_failure(
                message,
                target_id=target_id,
                processing=processing,
                error_type=type(exc).__name__,
                media_latency_ms=media_latency_ms,
                reasoning_effort=self.settings.ai_reasoning_effort,
                total_latency_ms=(
                    media_latency_ms + max(0, int((time.monotonic() - review_started) * 1000))
                ),
            )
            return

        processing = await processing_task if processing_task is not None else None
        await self._note_gateway_success(cached=result.cached)
        if await self._finish_if_ai_paused(message, processing):
            await self._persist_ai_learning(
                chat_id=message.chat.id,
                result=result,
                text=text,
                image_data_url=image_data_url,
                risk_signals=normalized_risk_signals,
            )
            await self.storage.record_ai_decision(
                chat_id=message.chat.id,
                message_id=message.message_id,
                target_id=target_id,
                is_ad=result.is_ad,
                confidence=result.confidence,
                category=result.category,
                model=self.settings.ai_model,
                latency_ms=result.latency_ms,
                cached=result.cached,
                enforced=False,
                **self._ai_decision_metrics(
                    result,
                    media_latency_ms=media_latency_ms,
                    action_latency_ms=0,
                    total_latency_ms=media_latency_ms + result.latency_ms,
                ),
            )
            return
        enforce = result.is_ad and result.confidence >= self.settings.ai_ad_threshold
        action_started = time.monotonic()
        if enforce:
            enforced = await self._handle_violation(
                message,
                user_id,
                Violation("ai_ad", ""),
                processing=processing,
            )
        else:
            enforced = False
            if processing is not None:
                await self._finish_processing(message, processing, "审核完成。")
        action_latency_ms = max(0, int((time.monotonic() - action_started) * 1000))

        await self._persist_ai_learning(
            chat_id=message.chat.id,
            result=result,
            text=text,
            image_data_url=image_data_url,
            risk_signals=normalized_risk_signals,
        )

        await self.storage.record_ai_decision(
            chat_id=message.chat.id,
            message_id=message.message_id,
            target_id=target_id,
            is_ad=result.is_ad,
            confidence=result.confidence,
            category=result.category,
            model=self.settings.ai_model,
            latency_ms=result.latency_ms,
            cached=result.cached,
            enforced=enforced,
            **self._ai_decision_metrics(
                result,
                media_latency_ms=media_latency_ms,
                action_latency_ms=action_latency_ms,
                total_latency_ms=media_latency_ms + result.latency_ms + action_latency_ms,
            ),
        )

    async def _finish_if_ai_paused(
        self,
        message: Message,
        processing: Message | None,
    ) -> bool:
        if await self.storage.runtime_setting(message.chat.id, "ai_paused") != "1":
            return False
        if processing is not None:
            await self._finish_processing(message, processing, "AI 审核已暂停，消息已保留。")
        return True

    @staticmethod
    def _ai_decision_metrics(
        result,
        *,
        media_latency_ms: int,
        action_latency_ms: int,
        total_latency_ms: int,
    ) -> AIDecisionMetrics:
        return {
            "reasoning_effort": str(getattr(result, "reasoning_effort", "high")),
            "service_tier": str(getattr(result, "service_tier", "default")),
            "review_path": str(getattr(result, "review_path", "gateway")),
            "media_latency_ms": media_latency_ms,
            "queue_latency_ms": int(getattr(result, "queue_latency_ms", 0)),
            "request_latency_ms": int(getattr(result, "request_latency_ms", result.latency_ms)),
            "action_latency_ms": action_latency_ms,
            "total_latency_ms": total_latency_ms,
            "attempt_count": int(getattr(result, "attempt_count", int(not result.cached))),
        }

    async def _note_gateway_success(self, *, cached: bool) -> None:
        if cached:
            return
        async with self._ai_failure_lock:
            self._ai_failure_streak = 0

    async def _note_gateway_failure(self) -> bool:
        async with self._ai_failure_lock:
            self._ai_failure_streak += 1
            return self._ai_failure_streak == 3

    async def _review_message(
        self,
        *,
        text: str,
        image_data_url: str | None,
        safety_identifier: str,
        risk_signals: tuple[str, ...],
        linked_hosts: tuple[str, ...],
        deadline: float,
    ):
        if self.ai_reviewer is None:
            raise RuntimeError("AI reviewer is unavailable")
        return await self.ai_reviewer.review(
            text=text,
            image_data_url=image_data_url,
            safety_identifier=safety_identifier,
            risk_signals=risk_signals,
            linked_hosts=linked_hosts,
            reasoning_effort=self.settings.ai_reasoning_effort,
            deadline=deadline,
        )

    async def _persist_ai_learning(
        self,
        *,
        chat_id: int,
        result,
        text: str,
        image_data_url: str | None,
        risk_signals: tuple[str, ...],
    ) -> None:
        if not result.is_ad:
            return
        evidence = set(risk_signals)
        if text.strip():
            evidence.add("text")
        if image_data_url is not None:
            evidence.add("image")
        try:
            await self.storage.record_ai_learning(
                chat_id=chat_id,
                category=result.category,
                evidence=tuple(signal for signal in AI_LEARNING_EVIDENCE if signal in evidence),
                confidence=result.confidence,
            )
            signature_status = (
                "active" if result.confidence >= self.settings.ai_learn_threshold else "candidate"
            )
            for signature in result.signatures:
                learned_signature = await self.storage.upsert_ai_signature(
                    chat_id=chat_id,
                    normalized_signature=signature,
                    display_signature=signature,
                    category=result.category,
                    status=signature_status,
                    confidence=result.confidence,
                )
                if (
                    learned_signature.status == "candidate"
                    and learned_signature.hit_count >= AI_CANDIDATE_PROMOTION_HITS
                    and learned_signature.max_confidence >= self.settings.ai_ad_threshold
                ):
                    await self.storage.activate_ai_signature(chat_id, learned_signature.id)
                    self._invalidate_policy(chat_id)
                if learned_signature.status == "active" or signature_status == "active":
                    self._invalidate_policy(chat_id)
            for signature in result.rejected_signatures:
                await self.storage.upsert_ai_signature(
                    chat_id=chat_id,
                    normalized_signature=signature,
                    display_signature=signature,
                    category=result.category,
                    status="rejected",
                    confidence=result.confidence,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AI 学习写入失败 chat_id=%s", chat_id)

    async def _moderate_legacy(
        self,
        message: Message,
        user_id: int | None,
        text: str,
        entity_urls: tuple[str, ...],
    ) -> None:
        allowed_domains, blocked_words = await asyncio.gather(
            self.storage.allowed_domains(message.chat.id),
            self.storage.blocked_words(message.chat.id),
        )
        violation = self.detector.detect(
            DetectionInput(
                text=text,
                entity_urls=entity_urls,
                is_forwarded=message.forward_origin is not None,
            ),
            allowed_domains=set(self.settings.allowed_domains) | allowed_domains,
            blocked_words=set(self.settings.blocked_words) | blocked_words,
        )
        if violation is None and user_id is not None:
            violation = self.flood.check(message.chat.id, user_id, text)
        if violation is not None:
            await self._handle_violation(message, user_id, violation)

    async def _message_image_data_url(self, message: Message) -> str | None:
        downloadable = None
        if message.photo:
            downloadable = select_photo_preview(
                list(message.photo),
                target_long_edge=self.settings.ai_image_target_long_edge,
                maximum_bytes=self.settings.ai_image_max_bytes,
            )
        elif message.video is not None:
            video_cover = getattr(message.video, "cover", None)
            if video_cover:
                downloadable = select_photo_preview(
                    list(video_cover),
                    target_long_edge=self.settings.ai_image_target_long_edge,
                    maximum_bytes=self.settings.ai_image_max_bytes,
                )
            if downloadable is None:
                downloadable = message.video.thumbnail
        if downloadable is None:
            return None
        if (
            downloadable.file_size is not None
            and downloadable.file_size > self.settings.ai_image_max_bytes
        ):
            return None
        destination = BoundedBytesIO(self.settings.ai_image_max_bytes)
        try:
            async with self._media_semaphore:
                await asyncio.wait_for(
                    self.bot.download(downloadable, destination=destination),
                    timeout=self.settings.ai_image_download_timeout_seconds,
                )
        except (
            TelegramAPIError,
            AiohttpClientError,
            asyncio.TimeoutError,
            OSError,
            MediaPreviewTooLarge,
        ):
            logger.warning("媒体预览下载失败，改用可用文字审核")
            return None
        content = destination.getvalue()
        if not content or len(content) > self.settings.ai_image_max_bytes:
            return None
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    async def _handle_ai_failure(
        self,
        message: Message,
        *,
        target_id: int | None,
        processing: Message | None,
        error_type: str,
        media_latency_ms: int = 0,
        reasoning_effort: str = "high",
        total_latency_ms: int = 0,
    ) -> None:
        should_alert = await self._note_gateway_failure()
        paused = await self.storage.runtime_setting(message.chat.id, "ai_paused") == "1"
        enforced = False
        delete_attempted = False
        if self.settings.ai_failure_delete and not self.settings.dry_run and not paused:
            delete_attempted = True
            try:
                await self.bot.delete_message(message.chat.id, message.message_id)
            except TelegramBadRequest as exc:
                if telegram_message_is_absent(exc):
                    enforced = True
                    await self.reporter.record(
                        chat_id=message.chat.id,
                        action="delete",
                        message_id=message.message_id,
                        target_id=target_id,
                    )
                else:
                    logger.exception(
                        "AI 故障保护删除失败 chat_id=%s message_id=%s",
                        message.chat.id,
                        message.message_id,
                    )
                    await self.reporter.record(
                        chat_id=message.chat.id,
                        action="delete_failed",
                        message_id=message.message_id,
                        target_id=target_id,
                    )
            except TelegramAPIError:
                logger.exception(
                    "AI 故障保护删除失败 chat_id=%s message_id=%s",
                    message.chat.id,
                    message.message_id,
                )
                await self.reporter.record(
                    chat_id=message.chat.id,
                    action="delete_failed",
                    message_id=message.message_id,
                    target_id=target_id,
                )
            else:
                enforced = True
                await self.reporter.record(
                    chat_id=message.chat.id,
                    action="delete",
                    message_id=message.message_id,
                    target_id=target_id,
                )
        await self.storage.record_ai_decision(
            chat_id=message.chat.id,
            message_id=message.message_id,
            target_id=target_id,
            is_ad=None,
            confidence=None,
            category="",
            model=self.settings.ai_model,
            latency_ms=0,
            cached=False,
            enforced=enforced,
            error_type=error_type,
            reasoning_effort=reasoning_effort,
            service_tier=self.settings.ai_service_tier,
            review_path=f"failure_{reasoning_effort}",
            media_latency_ms=media_latency_ms,
            total_latency_ms=total_latency_ms,
            request_latency_ms=max(0, total_latency_ms - media_latency_ms),
            attempt_count=1,
        )
        if paused:
            result_text = "AI 审核已暂停，消息已保留。"
        elif self.settings.dry_run:
            result_text = "审核服务异常，观察模式下消息已保留。"
        elif enforced:
            result_text = "审核服务异常，消息已删除。"
        elif delete_attempted:
            result_text = "审核服务异常，消息删除失败。"
        else:
            result_text = "审核服务异常，消息已保留。"
        if processing is not None or not paused:
            await self._finish_processing(message, processing, result_text)
        if should_alert:
            await self._alert_ai_failures()

    async def _alert_ai_failures(self) -> None:
        recipients = set(self.settings.authorized_admin_ids)
        if not recipients and self.settings.log_chat_id is not None:
            recipients.add(self.settings.log_chat_id)
        for recipient in recipients:
            try:
                await self.bot.send_message(recipient, "AI 审核已连续失败 3 次，请立即检查网关。")
            except TelegramAPIError:
                logger.exception("发送 AI 连续失败告警失败 recipient=%s", recipient)

    def _member_action_lock(self, chat_id: int, user_id: int) -> asyncio.Lock:
        key = (chat_id, user_id)
        lock = self._member_action_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._member_action_locks[key] = lock
        return lock

    def _remember_auto_banned(self, key: tuple[int, int]) -> None:
        now = time.monotonic()
        while self._auto_banned_members:
            oldest_key, expires_at = next(iter(self._auto_banned_members.items()))
            if expires_at > now:
                break
            self._auto_banned_members.pop(oldest_key, None)
        self._auto_banned_members[key] = now + AUTO_BAN_MEMORY_SECONDS
        self._auto_banned_members.move_to_end(key)
        while len(self._auto_banned_members) > AUTO_BAN_MEMORY_MAX:
            self._auto_banned_members.popitem(last=False)

    def _is_auto_banned(self, key: tuple[int, int]) -> bool:
        expires_at = self._auto_banned_members.get(key)
        if expires_at is None:
            return False
        if expires_at <= time.monotonic():
            self._auto_banned_members.pop(key, None)
            return False
        self._auto_banned_members.move_to_end(key)
        return True

    def _forget_auto_banned(self, key: tuple[int, int]) -> None:
        self._auto_banned_members.pop(key, None)

    async def _warn_and_maybe_ban(
        self,
        *,
        chat_id: int,
        user_id: int,
        actor_id: int | None = None,
    ) -> tuple[int, bool, str | None]:
        key = (chat_id, user_id)
        async with self._member_action_lock(chat_id, user_id):
            if self._is_auto_banned(key):
                return self.settings.warn_limit, True, None

            count = await self.storage.add_warning_and_record_action(
                chat_id,
                user_id,
                actor_id,
                enqueue_log=self.settings.log_chat_id is not None,
            )
            logger.info(
                "moderation action=warn chat_id=%s actor_id=%s target_id=%s",
                chat_id,
                actor_id,
                user_id,
            )
            if count < self.settings.warn_limit:
                return count, False, None

            try:
                await self.bot.ban_chat_member(
                    chat_id,
                    user_id,
                    revoke_messages=True,
                )
            except TelegramAPIError as exc:
                logger.exception("自动封禁失败 chat_id=%s user_id=%s", chat_id, user_id)
                return count, False, str(exc)

            self._remember_auto_banned(key)
            await self.storage.clear_warnings(chat_id, user_id)
            await self.reporter.record(
                chat_id=chat_id,
                action="auto_ban",
                actor_id=actor_id,
                target_id=user_id,
            )
            return count, True, None

    async def _handle_violation(
        self,
        message: Message,
        user_id: int | None,
        _violation: Violation,
        *,
        processing: Message | None = None,
    ) -> bool:
        target_id = user_id or (message.sender_chat.id if message.sender_chat else None)
        if self.settings.dry_run:
            await self.reporter.record(
                chat_id=message.chat.id,
                action="dry_run_hit",
                message_id=message.message_id,
                target_id=target_id,
            )
            if processing is not None:
                await self._finish_processing(message, processing, "观察模式命中，未执行。")
            return False

        try:
            await self.bot.delete_message(message.chat.id, message.message_id)
        except TelegramBadRequest as exc:
            if telegram_message_is_absent(exc):
                logger.info(
                    "自动删除目标已不存在，继续处罚 chat_id=%s message_id=%s",
                    message.chat.id,
                    message.message_id,
                )
            else:
                logger.exception(
                    "自动删除失败 chat_id=%s message_id=%s",
                    message.chat.id,
                    message.message_id,
                )
                await self.reporter.record(
                    chat_id=message.chat.id,
                    action="delete_failed",
                    message_id=message.message_id,
                    target_id=target_id,
                )
                await self._finish_processing(message, processing, "广告消息删除失败。")
                return False
        except TelegramAPIError:
            logger.exception(
                "自动删除失败 chat_id=%s message_id=%s", message.chat.id, message.message_id
            )
            await self.reporter.record(
                chat_id=message.chat.id,
                action="delete_failed",
                message_id=message.message_id,
                target_id=target_id,
            )
            await self._finish_processing(message, processing, "广告消息删除失败。")
            return False

        await self.reporter.record(
            chat_id=message.chat.id,
            action="delete",
            message_id=message.message_id,
            target_id=target_id,
        )
        if user_id is None:
            await self._finish_processing(message, processing, "广告内容已删除。")
            return True

        count, banned, ban_error = await self._warn_and_maybe_ban(
            chat_id=message.chat.id,
            user_id=user_id,
        )

        if banned:
            result_text = "广告内容已删除，用户已封禁。"
        elif ban_error is not None:
            result_text = "广告内容已删除，自动封禁失败。"
        else:
            result_text = f"广告内容已删除，警告 {count}/{self.settings.warn_limit}。"
        await self._finish_processing(message, processing, result_text)
        return True

    async def chat_member_changed(self, event: ChatMemberUpdated) -> None:
        user = event.new_chat_member.user
        raw_status = event.new_chat_member.status
        status = str(getattr(raw_status, "value", raw_status))
        is_admin = status in {
            ChatMemberStatus.CREATOR.value,
            ChatMemberStatus.ADMINISTRATOR.value,
        }
        self.admins.update_member(event.chat.id, user.id, is_admin=is_admin)
        key = (event.chat.id, user.id)
        if status == ChatMemberStatus.KICKED.value:
            self._remember_auto_banned(key)
        else:
            self._forget_auto_banned(key)
