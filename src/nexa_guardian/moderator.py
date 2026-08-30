from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import io
import logging
import secrets
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict, TypeVar
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    ChatMemberAdministrator,
    ChatMemberOwner,
    ChatMemberUpdated,
    ChatPermissions,
    Message,
    PhotoSize,
    User,
)
from aiohttp import ClientError as AiohttpClientError

from nexa_guardian.admin import AdminRights, AdminService
from nexa_guardian.ai_review import (
    AIReviewer,
    AIReviewError,
    normalize_signature,
    suspicious_precheck,
)
from nexa_guardian.commands import (
    command_arguments,
    command_payload,
    duration_from,
    resolve_target,
)
from nexa_guardian.config import Settings
from nexa_guardian.detection import (
    AdDetector,
    DetectionInput,
    FloodGuard,
    Violation,
    domain_from_url,
    extract_entity_urls,
    human_duration,
    normalize_domain,
    parse_duration,
)
from nexa_guardian.history_purge import (
    HistoryPurgeError,
    HistoryPurger,
    TelegramAdminSession,
)
from nexa_guardian.member_cleanup import (
    REASON_DELETED,
    REASON_LAST_MONTH,
    REASON_OFFLINE,
    REASON_SILENT,
    MemberCandidate,
    MemberScanError,
    MemberScanner,
)
from nexa_guardian.storage import (
    AIStats,
    ChatStats,
    LogOutboxRecord,
    MemberActivity,
    Raffle,
    Storage,
    normalize_raffle_keyword,
)

logger = logging.getLogger(__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
AUTO_BAN_MEMORY_SECONDS = 60 * 60
AUTO_BAN_MEMORY_MAX = 10_000
AI_CANDIDATE_PROMOTION_HITS = 3
MEMBER_CLEANUP_DAYS = 30
INACTIVE_CLEANUP_LIMIT = 50
INACTIVE_CONFIRM_TTL_SECONDS = 10 * 60
INACTIVE_KICK_FALLBACK_SECONDS = 5 * 60
INACTIVE_RETRY_AFTER_MAX_SECONDS = 15
RAFFLE_MIN_SECONDS = 60
RAFFLE_MAX_SECONDS = 30 * 24 * 60 * 60
RAFFLE_WORKER_INTERVAL_SECONDS = 2
RAFFLE_CARD_DEBOUNCE_SECONDS = 2
MODERATION_POLICY_CACHE_SECONDS = 5
TRUST_CACHE_SECONDS = 60
TRUST_CACHE_MAX = 4096
BOT_PROFILE_NAME = "Nexa 群管助手"
BOT_PROFILE_SHORT_DESCRIPTION = (
    "Nexa 官方群管理审核员：GPT-5.6 Sol 智能识别广告与刷屏，自动删除并警告；"
    "第 3 次直接封禁，并支持安全透明的群抽奖。"
)
BOT_PROFILE_DESCRIPTION = """Nexa Guardian 是 @ExampleGroup 的官方群管理与安全审核员。

• GPT-5.6 Sol 审核文字、图片及视频封面
• 自动清理广告、诈骗、恶意推广与刷屏；第 3 次警告直接封禁
• 普通聊天静默处理；仅可疑消息显示审核状态
• 持续学习高置信广告特征，提升后续拦截效率
• 极简管理入口：状态、删除、封禁与群抽奖
• 抽奖支持关键词报名、自动置顶、定时开奖、取消及重抽
• 群内机器人回复约 30 秒后清理；管理员私聊回复永久保留
• 不保存群消息原文、图片或模型推理

发送 /help 查看管理命令。

官方服务：
Example Store 商城：https://example.org
Example · Pass：https://pass.example.com
Example · Gift：https://gift.example.com"""
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
TelegramCallResult = TypeVar("TelegramCallResult")


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
class PendingMemberCleanup:
    actor_id: int
    chat_id: int
    cutoff: datetime
    created_at: datetime
    members: tuple[MemberCandidate, ...]
    expires_at: float


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


async def telegram_call_with_bounded_retry(
    operation: Callable[[], Awaitable[TelegramCallResult]],
) -> TelegramCallResult:
    """Retry one Telegram flood-control response without stalling indefinitely."""

    try:
        return await operation()
    except TelegramRetryAfter as exc:
        if exc.retry_after > INACTIVE_RETRY_AFTER_MAX_SECONDS:
            raise
        await asyncio.sleep(max(0, exc.retry_after) + 0.1)
        return await operation()


ACTION_LABELS = {
    "delete": "删除消息",
    "warn": "警告",
    "mute": "禁言",
    "auto_mute": "自动禁言",
    "auto_ban": "警告达限自动封禁",
    "unmute": "解除禁言",
    "ban": "封禁",
    "unban": "解除封禁",
    "kick": "移出群组",
    "kick_unban_failed": "移出后解封失败",
    "clear_warnings": "清除警告",
    "trust": "添加可信用户",
    "untrust": "移除可信用户",
    "add_words": "添加拦截词",
    "add_word": "添加拦截词",
    "delete_words": "移除拦截词",
    "add_domain": "添加域名白名单",
    "delete_domain": "移除域名白名单",
    "dry_run_hit": "观察模式命中",
    "delete_failed": "删除失败",
    "ai_signature_delete": "AI 特征命中删除",
    "ai_failure_delete": "AI 故障保护删除",
    "cleanup_inactive": "清理长期未发言成员",
    "cleanup_inactive_unban_failed": "清理成员后解除封禁失败",
}

HELP_TEXT = """<b>Nexa Guardian</b>

广告由 GPT-5.6 Sol 自动审核；累计 3 次警告自动封禁。

<code>/status</code>　查看机器人与抽奖状态
<code>/ban</code>　回复成员消息后封禁
<code>/del</code>　回复消息后删除
<code>/del all</code>　清理全部群历史
<code>/del members</code>　私聊预览长期不活跃成员
<code>/del members 确认码 confirm</code>　确认移出候选成员
<code>/lottery 30m 1 参与 奖品</code>　创建关键词抽奖
<code>/lottery end|cancel|reroll</code>　管理抽奖

私聊仅唯一授权管理员可用；群内普通回复约 30 秒后清理。"""


def display_user(user: User | None, user_id: int) -> str:
    if user is None:
        return f"<code>{user_id}</code>"
    name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def stats_cutoffs(now: datetime | None = None) -> tuple[str, str]:
    current = now or datetime.now(UTC)
    local = current.astimezone(DISPLAY_TIMEZONE)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=6)
    return (
        today.astimezone(UTC).isoformat(timespec="seconds"),
        week.astimezone(UTC).isoformat(timespec="seconds"),
    )


def action_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%m-%d %H:%M")


def telegram_message_is_absent(error: TelegramBadRequest) -> bool:
    return "message to delete not found" in str(error).casefold()


def stats_line(label: str, stats: ChatStats) -> str:
    return (
        f"{label}：删除 {stats.deleted}｜警告 {stats.warned}｜"
        f"禁言 {stats.muted}｜封禁 {stats.banned}"
    )


def fallback_send_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_react_to_messages=True,
    )


def muted_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_react_to_messages=False,
    )


class ActionReporter:
    def __init__(self, bot: Bot, storage: Storage, log_chat_id: int | None) -> None:
        self.bot = bot
        self.storage = storage
        self.log_chat_id = log_chat_id
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self.log_chat_id is None:
            return
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stopping = False
        self._worker_task = asyncio.create_task(
            self._run_worker(),
            name="nexa-moderation-log-outbox",
        )

    async def shutdown(self) -> None:
        self._stopping = True
        task = self._worker_task
        self._worker_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def record(
        self,
        *,
        chat_id: int,
        action: str,
        message_id: int | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
    ) -> None:
        await self.storage.record_action(
            chat_id=chat_id,
            action=action,
            message_id=message_id,
            actor_id=actor_id,
            target_id=target_id,
            enqueue_log=self.log_chat_id is not None,
        )
        logger.info(
            "moderation action=%s chat_id=%s actor_id=%s target_id=%s message_id=%s",
            action,
            chat_id,
            actor_id,
            target_id,
            message_id,
        )

    async def _deliver(self, record: LogOutboxRecord) -> None:
        if self.log_chat_id is None:
            return
        text = (
            f"<b>群管日志</b>\n"
            f"动作：<code>{html.escape(record.action)}</code>\n"
            f"群：<code>{record.chat_id}</code>\n"
            f"目标：<code>{record.target_id or '-'}</code>\n"
            f"操作者：<code>{record.actor_id or '自动'}</code>"
        )
        try:
            await self.bot.send_message(self.log_chat_id, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = min(300, 2 ** min(record.attempt_count + 1, 8))
            due_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds")
            await self.storage.reschedule_log_outbox(record.id, due_at)
            logger.exception(
                "发送群管日志失败，已进入重试 log_chat_id=%s outbox_id=%s",
                self.log_chat_id,
                record.id,
            )
        else:
            await self.storage.ack_log_outbox(record.id)

    async def _run_worker(self) -> None:
        while True:
            try:
                now = datetime.now(UTC)
                records = await self.storage.claim_due_log_outbox(
                    now.isoformat(timespec="seconds"),
                    lease_until=(now + timedelta(seconds=30)).isoformat(timespec="seconds"),
                    limit=20,
                )
                if records:
                    await asyncio.gather(*(self._deliver(record) for record in records))
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                logger.error("群管日志 outbox worker 被意外取消，继续运行")
            except Exception:
                logger.exception("群管日志 outbox worker 迭代失败")
                await asyncio.sleep(1)


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
        self._member_cleanup_plans: dict[tuple[int, str], PendingMemberCleanup] = {}
        self._policy_cache: dict[int, ModerationPolicySnapshot] = {}
        self._policy_locks: dict[int, asyncio.Lock] = {}
        self._policy_versions: dict[int, int] = {}
        self._trusted_cache: OrderedDict[tuple[int, int], tuple[float, bool]] = OrderedDict()
        self._raffle_locks: dict[int, asyncio.Lock] = {}
        self._raffle_card_updates: dict[int, tuple[Raffle, int]] = {}
        self._raffle_card_tasks: dict[int, asyncio.Task[None]] = {}
        self._raffle_worker_task: asyncio.Task[None] | None = None
        self._stopping = False
        telegram_admin_session = TelegramAdminSession(
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            session_path=settings.telegram_session_path,
            authorized_user_ids=settings.authorized_admin_ids,
        )
        self.history_purger = HistoryPurger(
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            session_path=settings.telegram_session_path,
            authorized_user_ids=settings.authorized_admin_ids,
            session=telegram_admin_session,
        )
        self.member_scanner = MemberScanner(
            telegram_admin_session,
            authorized_user_ids=settings.authorized_admin_ids,
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        # Private updates from anyone except the single configured administrator
        # are silently consumed before any command handler can respond.
        self.router.message.register(
            self._ignore_unauthorized_private,
            self._unauthorized_private_message,
        )
        self.router.message.register(
            self._delete_raffle_pin_service_message,
            self._raffle_pin_service_message,
        )
        self.router.message.register(
            self.join_lottery_keyword,
            self._raffle_keyword_message,
        )
        # Member-authored command-looking messages are still untrusted content.
        # Register this guard before Telegram command handlers so only verified
        # admins (and other moderation exemptions) can reach the command path.
        self.router.message.register(
            self.moderate_message,
            self._ordinary_member_command,
        )
        self.router.message.register(self.help, Command("help"))
        self.router.message.register(self.status, Command("status"))
        self.router.message.register(self.ban, Command("ban"))
        self.router.message.register(self.delete_replied, Command("del"))
        self.router.message.register(self.lottery, Command("lottery"))
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
        commands = [
            BotCommand(command="help", description="查看极简使用说明"),
            BotCommand(command="status", description="查看机器人与抽奖状态"),
            BotCommand(command="del", description="删除消息；all 清理群历史"),
            BotCommand(command="ban", description="封禁：/ban 用户ID"),
            BotCommand(command="lottery", description="创建或管理群抽奖"),
        ]
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
        self._stopping = False
        await self.reporter.start()
        await self._clean_pending_raffles()
        await self._sync_active_raffle_cards()
        self._raffle_worker_task = asyncio.create_task(
            self._raffle_worker(), name="nexa-raffle-worker"
        )

    async def shutdown(self) -> None:
        self._stopping = True
        if self._raffle_worker_task is not None:
            self._raffle_worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._raffle_worker_task
            self._raffle_worker_task = None
        await self._flush_raffle_card_updates()
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

    def _invalidate_trusted(self, chat_id: int, user_id: int) -> None:
        self._trusted_cache.pop((chat_id, user_id), None)

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
                "此群尚未加入 <code>ALLOWED_CHAT_IDS</code>，先用 /chatid 获取群 ID。"
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

    async def _send_chunked_list(
        self,
        message: Message,
        *,
        title: str,
        items: list[str],
        inline: bool = False,
        max_length: int = 3500,
    ) -> None:
        if not items:
            await message.answer(f"<b>{html.escape(title)}（0）</b>\n（空）")
            return
        separator = " · " if inline else "\n"
        bodies: list[str] = []
        current = ""
        for item in items:
            entry = f"<code>{html.escape(item)}</code>"
            if not inline:
                entry = f"• {entry}"
            addition = entry if not current else f"{separator}{entry}"
            if current and len(current) + len(addition) > max_length:
                bodies.append(current)
                current = entry
                continue
            current += addition
        if current:
            bodies.append(current)
        for index, body in enumerate(bodies, start=1):
            page = f" {index}/{len(bodies)}" if len(bodies) > 1 else ""
            await message.answer(f"<b>{html.escape(title)}（{len(items)}）{page}</b>\n{body}")

    async def _mute_member(self, chat_id: int, user_id: int, seconds: int) -> None:
        until = datetime.now(UTC) + timedelta(seconds=seconds)
        await self.bot.restrict_chat_member(
            chat_id,
            user_id,
            muted_permissions(),
            use_independent_chat_permissions=True,
            until_date=until,
        )

    async def _unmute_member(self, chat_id: int, user_id: int) -> None:
        chat = await self.bot.get_chat(chat_id)
        permissions = chat.permissions or fallback_send_permissions()
        await self.bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions,
            use_independent_chat_permissions=True,
        )

    async def start(self, message: Message) -> None:
        if message.chat.type == ChatType.PRIVATE:
            await self.help(message)
            return
        await self.help(message)

    async def help(self, message: Message) -> None:
        if await self._group_admin(message) is not None:
            await message.answer(HELP_TEXT)

    async def chat_id(self, message: Message) -> None:
        actor = await self._group_admin(message, require_allowed=False)
        if actor is None:
            return
        chat_id = actor[2]
        enabled = "已启用" if self.is_allowed_chat(chat_id) else "未启用"
        await message.answer(f"群 ID：<code>{chat_id}</code>\n自动群管：{enabled}")

    async def status(self, message: Message) -> None:
        actor = await self._group_admin(message, require_allowed=False)
        if actor is None:
            return
        chat_id = actor[2]
        me = await self.bot.get_me()
        member = await self.bot.get_chat_member(chat_id, me.id)
        is_admin = isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
        can_delete = isinstance(member, ChatMemberOwner) or (
            isinstance(member, ChatMemberAdministrator) and member.can_delete_messages
        )
        can_restrict = isinstance(member, ChatMemberOwner) or (
            isinstance(member, ChatMemberAdministrator) and member.can_restrict_members
        )
        allowed = self.is_allowed_chat(chat_id)
        mode = "观察模式（不执行自动动作）" if self.settings.dry_run else "执行模式"
        raffle = await self.storage.active_raffle(chat_id)
        raffle_status = (
            f"进行中 #{raffle.id}，关键词 {html.escape(raffle.keyword or '-')}，"
            f"{await self.storage.raffle_entry_count(raffle.id)} 人参与，"
            f"{action_time(raffle.ends_at)} 开奖"
            if raffle is not None
            else "无进行中抽奖"
        )
        health_warnings: list[str] = []
        if self._ai_failure_streak:
            health_warnings.append(f"AI 连续失败 {self._ai_failure_streak} 次")
        overdue = await self.storage.due_ephemeral(
            datetime.now(UTC).isoformat(timespec="seconds"),
            limit=1,
        )
        if overdue:
            health_warnings.append("临时消息清理存在积压")
        health_line = (
            f"\n运行告警：{html.escape('；'.join(health_warnings))}" if health_warnings else ""
        )
        await message.answer(
            f"<b>运行状态</b>\n"
            f"机器人：@{html.escape(me.username or str(me.id))}\n"
            f"目标群：<code>{chat_id}</code>\n"
            f"群白名单：{'是' if allowed else '否'}\n"
            f"自动群管：{'启用' if allowed else '停用'}\n"
            f"模式：{mode}\n"
            f"机器人管理员：{'是' if is_admin else '否'}\n"
            f"删除消息权限：{'有' if can_delete else '缺少'}\n"
            f"限制成员权限：{'有' if can_restrict else '缺少'}\n"
            f"AI 审核：{'开' if self.settings.ai_enabled else '关'}\n"
            f"抽奖：{raffle_status}\n"
            f"MTProto 管理："
            f"{'已配置' if self.settings.mtproto_ready else '待配置 Telegram API 凭据'}"
            f"{health_line}"
        )

    @staticmethod
    def _raffle_card(raffle: Raffle, count: int) -> str:
        ends = action_time(raffle.ends_at)
        return (
            "<b>🎁 Nexa 群抽奖</b>\n\n"
            f"奖品：{html.escape(raffle.prize)}\n"
            f"参与关键词：<code>{html.escape(raffle.keyword or '')}</code>\n"
            f"中奖人数：{raffle.winner_count}\n"
            f"参与人数：{count}\n"
            f"开奖时间：{ends}（北京时间）"
        )

    async def _raffle_keyword_message(self, message: Message) -> bool | dict[str, Raffle]:
        anonymous_admin = (
            message.sender_chat is not None and message.sender_chat.id == message.chat.id
        )
        if (
            message.chat.type not in GROUP_TYPES
            or not self.is_allowed_chat(message.chat.id)
            or message.text is None
            or message.is_automatic_forward
        ):
            return False
        if not anonymous_admin and (
            message.from_user is None or message.from_user.is_bot or message.sender_chat is not None
        ):
            return False
        try:
            normalized = normalize_raffle_keyword(message.text)
        except ValueError:
            return False
        raffle = await self.storage.active_raffle_for_keyword(message.chat.id, normalized)
        return {"raffle": raffle} if raffle is not None else False

    async def _raffle_pin_service_message(self, message: Message) -> bool:
        pinned = message.pinned_message
        if (
            pinned is None
            or message.chat.type not in GROUP_TYPES
            or not self.is_allowed_chat(message.chat.id)
        ):
            return False
        return await self.storage.raffle_for_message(message.chat.id, pinned.message_id) is not None

    async def _delete_raffle_pin_service_message(self, message: Message) -> None:
        try:
            await self.bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            logger.debug("删除抽奖置顶服务消息失败 chat_id=%s", message.chat.id)

    async def join_lottery_keyword(self, message: Message, raffle: Raffle) -> None:
        anonymous_admin = (
            message.sender_chat is not None and message.sender_chat.id == message.chat.id
        )
        if anonymous_admin:
            try:
                actor = await self.admins.unique_anonymous_actor(
                    message.chat.id, self.settings.authorized_admin_ids
                )
            except TelegramAPIError:
                actor = None
                logger.exception("核验匿名抽奖参与者失败 chat_id=%s", message.chat.id)
            if actor is None:
                await self._delete_raffle_keyword_message(message)
                await self.bot.send_message(message.chat.id, "无法唯一核验匿名管理员，本次未登记。")
                return
            participant_id = actor[0]
            participant_user = None
        else:
            if message.from_user is None or message.from_user.is_bot:
                return
            participant_id = message.from_user.id
            participant_user = message.from_user

        joined, count = await self.storage.join_raffle(raffle.id, participant_id)
        await self._delete_raffle_keyword_message(message)
        if count == 0:
            await self.bot.send_message(message.chat.id, "该抽奖已结束。")
            return
        if joined and raffle.message_id is not None:
            self._schedule_raffle_card_update(raffle, count)
        mention = self._raffle_participant_mention(participant_user, participant_id)
        text = (
            f"{mention}，已成功参与抽奖！🎉\n当前共有 {count} 人参与。"
            if joined
            else f"{mention}，你已经参与过啦，本次不会重复登记。"
        )
        await self.bot.send_message(message.chat.id, text)

    def _schedule_raffle_card_update(self, raffle: Raffle, count: int) -> None:
        previous = self._raffle_card_updates.get(raffle.id)
        if previous is None or count >= previous[1]:
            self._raffle_card_updates[raffle.id] = (raffle, count)
        task = self._raffle_card_tasks.get(raffle.id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._debounced_raffle_card_update(raffle.id),
                name=f"nexa-raffle-card-{raffle.id}",
            )
            self._raffle_card_tasks[raffle.id] = task
            task.add_done_callback(
                lambda completed, raffle_id=raffle.id: self._raffle_card_update_done(
                    raffle_id, completed
                )
            )

    async def _debounced_raffle_card_update(self, raffle_id: int) -> None:
        await asyncio.sleep(RAFFLE_CARD_DEBOUNCE_SECONDS)
        update = self._raffle_card_updates.pop(raffle_id, None)
        if update is not None:
            await self._edit_raffle_card(*update)

    def _raffle_card_update_done(self, raffle_id: int, task: asyncio.Task[None]) -> None:
        if self._raffle_card_tasks.get(raffle_id) is task:
            self._raffle_card_tasks.pop(raffle_id, None)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "抽奖人数更新任务失败 raffle_id=%s",
                raffle_id,
                exc_info=(type(exception), exception, exception.__traceback__),
            )
        pending = self._raffle_card_updates.get(raffle_id)
        if pending is not None and not self._stopping:
            self._schedule_raffle_card_update(*pending)

    async def _edit_raffle_card(self, raffle: Raffle, count: int) -> None:
        if raffle.message_id is None:
            return
        try:
            await self.bot.edit_message_text(
                self._raffle_card(raffle, count),
                chat_id=raffle.chat_id,
                message_id=raffle.message_id,
            )
            await self.storage.ack_ephemeral(raffle.chat_id, raffle.message_id)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).casefold():
                logger.debug("更新抽奖人数失败 raffle_id=%s", raffle.id)
        except TelegramAPIError:
            logger.debug("更新抽奖人数失败 raffle_id=%s", raffle.id)

    async def _sync_active_raffle_cards(self) -> None:
        for chat_id in self.settings.allowed_chat_ids:
            raffle = await self.storage.active_raffle(chat_id)
            if raffle is not None:
                await self._edit_raffle_card(
                    raffle,
                    await self.storage.raffle_entry_count(raffle.id),
                )

    async def _flush_raffle_card_updates(self) -> None:
        tasks = tuple(self._raffle_card_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._raffle_card_tasks.clear()
        updates = tuple(self._raffle_card_updates.values())
        self._raffle_card_updates.clear()
        for raffle, count in updates:
            await self._edit_raffle_card(raffle, count)

    async def _cancel_raffle_card_update(self, raffle_id: int) -> None:
        self._raffle_card_updates.pop(raffle_id, None)
        task = self._raffle_card_tasks.pop(raffle_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @staticmethod
    def _raffle_participant_mention(user: User | None, user_id: int) -> str:
        if user is not None and user.username:
            return f"@{html.escape(user.username)}"
        label = html.escape(user.full_name) if user is not None else "用户"
        return f'<a href="tg://user?id={user_id}">{label}</a>'

    async def _delete_raffle_keyword_message(self, message: Message) -> None:
        try:
            await self.bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            logger.debug(
                "删除抽奖关键词消息失败 chat_id=%s message_id=%s",
                message.chat.id,
                message.message_id,
            )

    async def lottery(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        payload = command_payload(message)
        if not payload:
            await message.answer(
                "用法：<code>/lottery 30m 1 参与 奖品</code>\n"
                "管理：<code>/lottery end|cancel|reroll</code>"
            )
            return
        operation = payload.casefold()
        if operation in {"end", "cancel", "reroll"}:
            if operation == "end":
                raffle = await self.storage.active_raffle(actor[2])
                if raffle is None:
                    await message.answer("当前没有进行中的抽奖。")
                    return
                await self._finalize_raffle(raffle, notify_admin=True)
                return
            if operation == "cancel":
                await self._cancel_active_raffle(actor[2])
                return
            await self._reroll_latest(actor[2])
            return

        parts = payload.split(maxsplit=3)
        if len(parts) != 4:
            await message.answer("格式：<code>/lottery 30m 1 参与 奖品名称</code>")
            return
        try:
            duration = parse_duration(parts[0])
            winner_count = int(parts[1])
        except ValueError:
            await message.answer("持续时间或中奖人数格式不正确。")
            return
        if not RAFFLE_MIN_SECONDS <= duration <= RAFFLE_MAX_SECONDS:
            await message.answer("抽奖持续时间必须为 1 分钟至 30 天。")
            return
        try:
            raffle = await self.storage.create_raffle(
                chat_id=actor[2],
                creator_id=actor[0],
                keyword=parts[2],
                prize=parts[3],
                winner_count=winner_count,
                ends_at=(datetime.now(UTC) + timedelta(seconds=duration)).isoformat(
                    timespec="seconds"
                ),
            )
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        card: Message | None = None
        pinned = False
        try:
            card = await self.bot.send_message(
                actor[2],
                self._raffle_card(raffle, 0),
            )
            await self.storage.ack_ephemeral(actor[2], card.message_id)
            if not await self.storage.set_raffle_message(raffle.id, card.message_id):
                raise RuntimeError("抽奖卡片绑定失败")
            pinned = bool(
                await self.bot.pin_chat_message(
                    actor[2], card.message_id, disable_notification=True
                )
            )
            if not pinned:
                raise RuntimeError("Telegram 未确认置顶")
            if not await self.storage.activate_raffle(raffle.id):
                raise RuntimeError("抽奖激活失败")
        except (TelegramAPIError, RuntimeError):
            if card is not None:
                if pinned:
                    with contextlib.suppress(TelegramAPIError):
                        await self.bot.unpin_chat_message(actor[2], message_id=card.message_id)
                with contextlib.suppress(TelegramAPIError):
                    await self.bot.delete_message(actor[2], card.message_id)
                await self.storage.ack_ephemeral(actor[2], card.message_id)
            await self.storage.discard_pending_raffle(raffle.id)
            logger.exception("发布抽奖卡片失败 chat_id=%s raffle_id=%s", actor[2], raffle.id)
            await message.answer("抽奖发布失败，已安全取消。")
            return
        await message.answer(
            "🎉 <b>抽奖已发布并置顶</b>\n"
            f"发送关键词 <code>{html.escape(raffle.keyword or '')}</code> 即可参与。"
        )

    async def _unpin_raffle_message(self, raffle: Raffle) -> bool:
        if raffle.message_id is None:
            return True
        try:
            return bool(
                await self.bot.unpin_chat_message(raffle.chat_id, message_id=raffle.message_id)
            )
        except TelegramBadRequest as exc:
            detail = str(exc).casefold()
            if (
                "message to unpin not found" in detail
                or "message not found" in detail
                or "message is not pinned" in detail
            ):
                return True
            logger.warning(
                "取消抽奖置顶失败 chat_id=%s raffle_id=%s error=%s",
                raffle.chat_id,
                raffle.id,
                type(exc).__name__,
            )
            return False
        except TelegramAPIError as exc:
            logger.warning(
                "取消抽奖置顶失败 chat_id=%s raffle_id=%s error=%s",
                raffle.chat_id,
                raffle.id,
                type(exc).__name__,
            )
            return False

    async def _clean_pending_raffles(self) -> None:
        for raffle in await self.storage.pending_raffles():
            if raffle.message_id is not None:
                await self._unpin_raffle_message(raffle)
                with contextlib.suppress(TelegramAPIError):
                    await self.bot.delete_message(raffle.chat_id, raffle.message_id)
                await self.storage.ack_ephemeral(raffle.chat_id, raffle.message_id)
            await self.storage.discard_pending_raffle(raffle.id)

    async def _schedule_raffle_card_cleanup(self, raffle: Raffle) -> None:
        if raffle.message_id is None:
            return
        delete_at = (
            datetime.now(UTC) + timedelta(seconds=self.settings.notice_ttl_seconds)
        ).isoformat(timespec="microseconds")
        try:
            await self.storage.enqueue_ephemeral(raffle.chat_id, raffle.message_id, delete_at)
        except Exception:
            logger.exception(
                "抽奖结果清理入队失败 chat_id=%s raffle_id=%s",
                raffle.chat_id,
                raffle.id,
            )

    async def _valid_raffle_entries(self, raffle: Raffle) -> tuple[int, ...]:
        user_ids = await self.storage.raffle_entry_ids(raffle.id)
        semaphore = asyncio.Semaphore(8)

        async def valid(user_id: int) -> int | None:
            async with semaphore:
                try:
                    member = await self.bot.get_chat_member(raffle.chat_id, user_id)
                except TelegramAPIError:
                    return None
                user = getattr(member, "user", None)
                if getattr(user, "is_bot", False):
                    return None
                if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} or (
                    member.status == ChatMemberStatus.RESTRICTED
                    and not bool(getattr(member, "is_member", False))
                ):
                    return None
                return user_id

        checked = await asyncio.gather(*(valid(user_id) for user_id in user_ids))
        return tuple(user_id for user_id in checked if user_id is not None)

    async def _finalize_raffle(self, raffle: Raffle, *, notify_admin: bool) -> None:
        lock = self._raffle_locks.setdefault(raffle.id, asyncio.Lock())
        async with lock:
            current = await self.storage.raffle(raffle.id)
            if current is None or current.status != "running":
                return
            candidates = await self._valid_raffle_entries(current)
            winner_total = min(current.winner_count, len(candidates))
            winners = tuple(secrets.SystemRandom().sample(candidates, winner_total))
            if not await self.storage.finish_raffle(current.id, winners):
                return
            await self._publish_raffle_result(current, winners, round_number=1)
            if notify_admin:
                await self._send_raffle_admin_result(current, winners, round_number=1)

    async def _publish_raffle_result(
        self, raffle: Raffle, winners: tuple[int, ...], *, round_number: int
    ) -> None:
        await self._cancel_raffle_card_update(raffle.id)
        if winners:
            lines = [
                f'{position}. <a href="tg://user?id={user_id}">中奖成员</a>'
                for position, user_id in enumerate(winners, start=1)
            ]
            result = (
                f"<b>🎉 抽奖结果 · 第 {round_number} 轮</b>\n\n"
                f"奖品：{html.escape(raffle.prize)}\n" + "\n".join(lines)
            )
        else:
            result = f"<b>抽奖已结束</b>\n\n奖品：{html.escape(raffle.prize)}\n无人符合条件。"
        await self._schedule_raffle_card_cleanup(raffle)
        await self._unpin_raffle_message(raffle)
        if raffle.message_id is not None:
            try:
                await self.bot.edit_message_text(
                    result,
                    chat_id=raffle.chat_id,
                    message_id=raffle.message_id,
                    reply_markup=None,
                )
                return
            except TelegramAPIError:
                logger.debug("编辑抽奖结果失败 raffle_id=%s", raffle.id)
        await self.bot.send_message(raffle.chat_id, result)

    async def _send_raffle_admin_result(
        self, raffle: Raffle, winners: tuple[int, ...], *, round_number: int
    ) -> None:
        if len(self.settings.authorized_admin_ids) != 1:
            return
        admin_id = next(iter(self.settings.authorized_admin_ids))
        ids = "、".join(str(user_id) for user_id in winners) if winners else "无"
        try:
            await self.bot.send_message(
                admin_id,
                f"抽奖 #{raffle.id} 第 {round_number} 轮完成。\n"
                f"奖品：{html.escape(raffle.prize)}\n中奖用户 ID：<code>{ids}</code>",
            )
        except TelegramAPIError:
            logger.debug("私聊发送抽奖结果失败 raffle_id=%s", raffle.id)

    async def _cancel_active_raffle(self, chat_id: int) -> None:
        raffle = await self.storage.active_raffle(chat_id)
        if raffle is None:
            await self.bot.send_message(chat_id, "当前没有进行中的抽奖。")
            return
        if not await self.storage.cancel_raffle(raffle.id):
            return
        await self._cancel_raffle_card_update(raffle.id)
        text = f"<b>抽奖已取消</b>\n\n奖品：{html.escape(raffle.prize)}"
        await self._schedule_raffle_card_cleanup(raffle)
        await self._unpin_raffle_message(raffle)
        if raffle.message_id is not None:
            try:
                await self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=raffle.message_id, reply_markup=None
                )
                return
            except TelegramAPIError:
                pass
        await self.bot.send_message(chat_id, text)

    async def _reroll_latest(self, chat_id: int) -> None:
        raffle = await self.storage.latest_finished_raffle(chat_id)
        if raffle is None:
            await self.bot.send_message(chat_id, "没有可重抽的历史抽奖。")
            return
        candidates = list(await self._valid_raffle_entries(raffle))
        previous = {winner.user_id for winner in await self.storage.raffle_winners(raffle.id)}
        preferred = [user_id for user_id in candidates if user_id not in previous]
        pool = preferred if len(preferred) >= raffle.winner_count else candidates
        winners = tuple(secrets.SystemRandom().sample(pool, min(raffle.winner_count, len(pool))))
        round_number = await self.storage.add_reroll(raffle.id, winners)
        await self._publish_raffle_result(raffle, winners, round_number=round_number)
        await self._send_raffle_admin_result(raffle, winners, round_number=round_number)

    async def _raffle_worker(self) -> None:
        while True:
            try:
                due = await self.storage.due_raffles(
                    datetime.now(UTC).isoformat(timespec="seconds")
                )
                for raffle in due:
                    await self._finalize_raffle(raffle, notify_admin=True)
                await asyncio.sleep(RAFFLE_WORKER_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                if self._stopping:
                    raise
            except Exception:
                logger.exception("抽奖后台任务执行失败")
                await asyncio.sleep(RAFFLE_WORKER_INTERVAL_SECONDS)

    async def settings_command(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        chat_id = actor[2]
        domains = set(self.settings.allowed_domains) | await self.storage.allowed_domains(chat_id)
        signatures = await self.storage.ai_signatures(chat_id, statuses=("active",))
        paused = await self.storage.runtime_setting(chat_id, "ai_paused") == "1"
        await message.answer(
            f"<b>当前群管规则</b>\n"
            f"AI 审核：{'暂停' if paused else '开' if self.settings.ai_enabled else '关'}\n"
            f"AI 模型：<code>{html.escape(self.settings.ai_model)}</code>\n"
            f"广告阈值：{self.settings.ai_ad_threshold:.2f}\n"
            f"学习阈值：{self.settings.ai_learn_threshold:.2f}\n"
            "外链/转发/@账号：仅作可疑预检\n"
            f"警告阈值：{self.settings.warn_limit}\n"
            "达到阈值：自动封禁\n"
            f"手动禁言默认：{human_duration(self.settings.default_mute_seconds)}\n"
            f"域名白名单：{len(domains)} 条\n"
            f"AI 已学习特征：{len(signatures)} 条\n"
            "人工关键词执行：关\n"
            f"授权管理员：{len(self.settings.authorized_admin_ids) or '全部群管理员'}\n"
            f"观察模式：{'开' if self.settings.dry_run else '关'}"
        )

    async def stats(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        today_since, week_since = stats_cutoffs()
        today, week, total, warnings = await asyncio.gather(
            self.storage.chat_stats(actor[2], since=today_since),
            self.storage.chat_stats(actor[2], since=week_since),
            self.storage.chat_stats(actor[2]),
            self.storage.warning_stats(actor[2]),
        )
        await message.answer(
            "<b>管理统计（北京时间）</b>\n"
            f"{stats_line('今天', today)}\n"
            f"{stats_line('近 7 天', week)}\n"
            f"{stats_line('累计', total)}\n"
            f"当前未清警告：{warnings.users} 人 / {warnings.total} 次"
        )

    async def recent(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("为避免公开用户 ID，请私聊机器人使用 /recent。")
            return
        arguments = command_arguments(message)
        if len(arguments) > 1:
            await message.answer("用法：<code>/recent [1-30]</code>")
            return
        try:
            limit = int(arguments[0]) if arguments else 10
        except ValueError:
            await message.answer("用法：<code>/recent [1-30]</code>")
            return
        if limit < 1 or limit > 30:
            await message.answer("最近操作数量必须在 1–30 之间。")
            return
        actions = await self.storage.recent_actions(actor[2], limit=limit)
        if not actions:
            await message.answer("<b>最近管理操作</b>\n（空）")
            return
        lines = []
        for action in actions:
            label = html.escape(ACTION_LABELS.get(action.action, action.action))
            operator = f"<code>{action.actor_id}</code>" if action.actor_id else "自动"
            target = f"<code>{action.target_id}</code>" if action.target_id else "—"
            lines.append(
                f"{action_time(action.created_at)}｜{label}｜操作者 {operator}｜目标 {target}"
            )
        await message.answer("<b>最近管理操作</b>\n" + "\n".join(lines))

    async def warn(self, message: Message) -> None:
        actor = await self._group_admin(message, restrict=True)
        if actor is None or (target := await self._safe_target(message, actor[2])) is None:
            return
        actor_id = actor[0]
        chat_id = actor[2]
        user_id, user, _ = target
        count, banned, ban_error = await self._warn_and_maybe_ban(
            chat_id=chat_id,
            user_id=user_id,
            actor_id=actor_id,
        )
        if ban_error is not None:
            await message.answer(f"警告已记录，但自动封禁失败：{html.escape(ban_error)}")
        suffix = "；达到阈值，已封禁" if banned else f"（{count}/{self.settings.warn_limit}）"
        await message.answer(f"已警告 {display_user(user, user_id)}{suffix}。")

    async def mute(self, message: Message) -> None:
        actor = await self._group_admin(message, restrict=True)
        if actor is None or (target := await self._safe_target(message, actor[2])) is None:
            return
        chat_id = actor[2]
        user_id, user, arguments = target
        try:
            seconds = duration_from(arguments, default_seconds=self.settings.default_mute_seconds)
            await self._mute_member(chat_id, user_id, seconds)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        except TelegramAPIError as exc:
            await message.answer(f"禁言失败：{html.escape(str(exc))}")
            return
        await self.reporter.record(
            chat_id=chat_id,
            action="mute",
            actor_id=actor[0],
            target_id=user_id,
        )
        await message.answer(f"已禁言 {display_user(user, user_id)} {human_duration(seconds)}。")

    async def unmute(self, message: Message) -> None:
        actor = await self._group_admin(message, restrict=True)
        if actor is None or (target := await self._safe_target(message, actor[2])) is None:
            return
        chat_id = actor[2]
        user_id, user, _ = target
        try:
            await self._unmute_member(chat_id, user_id)
        except TelegramAPIError as exc:
            await message.answer(f"解除禁言失败：{html.escape(str(exc))}")
            return
        await self.reporter.record(
            chat_id=chat_id,
            action="unmute",
            actor_id=actor[0],
            target_id=user_id,
        )
        await message.answer(f"已解除 {display_user(user, user_id)} 的禁言。")

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

    async def unban(self, message: Message) -> None:
        actor = await self._group_admin(message, restrict=True)
        if actor is None:
            return
        chat_id = actor[2]
        try:
            target = resolve_target(message)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        key = (chat_id, target.user_id)
        async with self._member_action_lock(chat_id, target.user_id):
            try:
                await self.bot.unban_chat_member(chat_id, target.user_id, only_if_banned=True)
            except TelegramAPIError as exc:
                await message.answer(f"解除封禁失败：{html.escape(str(exc))}")
                return
            self._forget_auto_banned(key)
        await self.reporter.record(
            chat_id=chat_id,
            action="unban",
            actor_id=actor[0],
            target_id=target.user_id,
        )
        await message.answer(f"已解除 <code>{target.user_id}</code> 的封禁。")

    async def kick(self, message: Message) -> None:
        actor = await self._group_admin(message, restrict=True)
        if actor is None or (target := await self._safe_target(message, actor[2])) is None:
            return
        chat_id = actor[2]
        user_id, user, _ = target
        banned = False
        key = (chat_id, user_id)
        async with self._member_action_lock(chat_id, user_id):
            try:
                await self.bot.ban_chat_member(chat_id, user_id, revoke_messages=False)
                banned = True
                self._remember_auto_banned(key)
                await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                self._forget_auto_banned(key)
            except TelegramAPIError as exc:
                if banned:
                    await self.reporter.record(
                        chat_id=chat_id,
                        action="kick_unban_failed",
                        actor_id=actor[0],
                        target_id=user_id,
                    )
                    await message.answer(
                        "已将成员封禁，但解除封禁步骤失败；该成员目前可能仍在封禁名单中。"
                        f"请核查后使用 /unban {user_id}。错误：{html.escape(str(exc))}"
                    )
                else:
                    await message.answer(f"移出失败：{html.escape(str(exc))}")
                return
        await self.reporter.record(
            chat_id=chat_id,
            action="kick",
            actor_id=actor[0],
            target_id=user_id,
        )
        await message.answer(f"已移出 {display_user(user, user_id)}。")

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
            await message.answer("正在清理群历史，期间群聊保持开放。")
            try:
                result = await self.history_purger.purge(chat_id)
            except HistoryPurgeError as exc:
                await message.answer(html.escape(str(exc)))
                return
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

    async def warnings(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        try:
            target = resolve_target(message)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        count = await self.storage.warning_count(actor[2], target.user_id)
        await message.answer(
            f"{display_user(target.user, target.user_id)} 当前警告："
            f"{count}/{self.settings.warn_limit}"
        )

    async def clear_warnings(self, message: Message) -> None:
        actor = await self._group_admin(message, restrict=True)
        if actor is None:
            return
        chat_id = actor[2]
        try:
            target = resolve_target(message)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        await self.storage.clear_warnings(chat_id, target.user_id)
        await self.reporter.record(
            chat_id=chat_id,
            action="clear_warnings",
            actor_id=actor[0],
            target_id=target.user_id,
        )
        await message.answer(f"已清除 {display_user(target.user, target.user_id)} 的警告。")

    async def trust(self, message: Message) -> None:
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        chat_id = actor[2]
        try:
            target = resolve_target(message)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        async with self._member_action_lock(chat_id, target.user_id):
            await self.storage.add_trusted_user(chat_id, target.user_id, actor[0])
        self._invalidate_trusted(chat_id, target.user_id)
        await self.reporter.record(
            chat_id=chat_id,
            action="trust",
            actor_id=actor[0],
            target_id=target.user_id,
        )
        await message.answer(f"已信任 {display_user(target.user, target.user_id)}。")

    async def untrust(self, message: Message) -> None:
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        chat_id = actor[2]
        try:
            target = resolve_target(message)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        async with self._member_action_lock(chat_id, target.user_id):
            await self.storage.remove_trusted_user(chat_id, target.user_id)
        self._invalidate_trusted(chat_id, target.user_id)
        await self.reporter.record(
            chat_id=chat_id,
            action="untrust",
            actor_id=actor[0],
            target_id=target.user_id,
        )
        await message.answer(f"已取消信任 {display_user(target.user, target.user_id)}。")

    async def add_domain(self, message: Message) -> None:
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        chat_id = actor[2]
        arguments = command_arguments(message)
        domain = normalize_domain(arguments[0]) if arguments else None
        if domain is None:
            await message.answer("用法：<code>/addomain example.com</code>")
            return
        await self.storage.add_allowed_domain(chat_id, domain, actor[0])
        self._invalidate_policy(chat_id)
        await self.reporter.record(
            chat_id=chat_id,
            action="add_domain",
            actor_id=actor[0],
        )
        await message.answer(f"已加入域名白名单：<code>{html.escape(domain)}</code>")

    async def delete_domain(self, message: Message) -> None:
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        chat_id = actor[2]
        arguments = command_arguments(message)
        domain = normalize_domain(arguments[0]) if arguments else None
        if domain is None:
            await message.answer("用法：<code>/deldomain example.com</code>")
            return
        if domain in {normalize_domain(item) for item in self.settings.allowed_domains}:
            await message.answer("这是环境变量中的全局白名单，请修改 .env 并重启。")
            return
        await self.storage.remove_allowed_domain(chat_id, domain)
        self._invalidate_policy(chat_id)
        await self.reporter.record(
            chat_id=chat_id,
            action="delete_domain",
            actor_id=actor[0],
        )
        await message.answer(f"已移除域名白名单：<code>{html.escape(domain)}</code>")

    async def list_domains(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        domains = {
            normalized
            for item in self.settings.allowed_domains
            if (normalized := normalize_domain(item)) is not None
        } | await self.storage.allowed_domains(actor[2])
        await self._send_chunked_list(message, title="域名白名单", items=sorted(domains))

    async def add_word(self, message: Message) -> None:
        if await self._group_admin(message, delete=True) is not None:
            await message.answer("当前使用 AI 自动学习模式，不接受人工添加拦截词。")

    async def delete_word(self, message: Message) -> None:
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        identifier = command_payload(message).strip()
        if not identifier:
            await message.answer("用法：<code>/delword 编号或完整特征</code>")
            return
        if not identifier.isdigit():
            identifier = normalize_signature(identifier)
        if not await self.storage.disable_ai_signature(actor[2], identifier):
            await message.answer("未找到可停用的 AI 特征。")
            return
        self._invalidate_policy(actor[2])
        await self.reporter.record(chat_id=actor[2], action="delete_words", actor_id=actor[0])
        await message.answer("已停用该 AI 特征。")

    async def list_words(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        signatures = await self.storage.ai_signatures(actor[2], statuses=("active",))
        await self._send_chunked_list(
            message,
            title="AI 已学习特征",
            items=[f"{item.id}｜{item.display_signature}" for item in signatures],
            inline=True,
        )

    async def ai_candidates(self, message: Message) -> None:
        actor = await self._private_ai_admin(message)
        if actor is None:
            return
        signatures = await self.storage.ai_signatures(actor[2], statuses=("candidate", "rejected"))
        await self._send_chunked_list(
            message,
            title="AI 候选特征",
            items=[f"{item.id}｜{item.status}｜{item.display_signature}" for item in signatures],
        )

    async def ai_stats(self, message: Message) -> None:
        actor = await self._private_ai_admin(message)
        if actor is None:
            return
        today_since, week_since = stats_cutoffs()
        today, week, total = await asyncio.gather(
            self.storage.ai_stats(actor[2], since=today_since),
            self.storage.ai_stats(actor[2], since=week_since),
            self.storage.ai_stats(actor[2]),
        )

        def line(label: str, stats: AIStats) -> str:
            failure_rate = (stats.failed / stats.calls * 100) if stats.calls else 0.0
            return (
                f"{label}：判断 {stats.total}｜调用 {stats.calls}｜广告 {stats.ads}｜"
                f"执行 {stats.enforced}｜失败率 {failure_rate:.1f}%｜"
                f"缓存 {stats.cached}｜均延迟 {stats.average_latency_ms}ms"
            )

        effort_counts = (
            " / ".join(f"{name} {count}" for name, count in week.reasoning_effort_counts) or "暂无"
        )
        path_counts = (
            " / ".join(f"{name} {count}" for name, count in week.review_path_counts) or "暂无"
        )

        await message.answer(
            "<b>AI 审核统计（北京时间）</b>\n"
            f"{line('今天', today)}\n{line('近 7 天', week)}\n{line('累计', total)}\n"
            f"近 7 天推理：{html.escape(effort_counts)}\n"
            f"近 7 天路径：{html.escape(path_counts)}\n"
            f"近 7 天延迟：网关 P50/P95 {week.gateway_latency_p50_ms}/"
            f"{week.gateway_latency_p95_ms}ms｜端到端 P50/P95 "
            f"{week.total_latency_p50_ms}/{week.total_latency_p95_ms}ms"
        )

    async def ai_recent(self, message: Message) -> None:
        actor = await self._private_ai_admin(message)
        if actor is None:
            return
        arguments = command_arguments(message)
        try:
            limit = int(arguments[0]) if arguments else 10
        except ValueError:
            limit = 0
        if len(arguments) > 1 or not 1 <= limit <= 30:
            await message.answer("用法：<code>/airecent [1-30]</code>")
            return
        decisions = await self.storage.recent_ai_decisions(actor[2], limit=limit)
        if not decisions:
            await message.answer("<b>最近 AI 判断</b>\n（空）")
            return
        lines = []
        for decision in decisions:
            verdict = "失败" if decision.is_ad is None else "广告" if decision.is_ad else "正常"
            confidence = "—" if decision.confidence is None else f"{decision.confidence:.2f}"
            lines.append(
                f"{action_time(decision.created_at)}｜{verdict}｜{confidence}｜"
                f"{'已执行' if decision.enforced else '未执行'}"
            )
        await message.answer("<b>最近 AI 判断</b>\n" + "\n".join(lines))

    async def ai_pause(self, message: Message) -> None:
        actor = await self._private_ai_admin(message)
        if actor is None:
            return
        await self.storage.set_runtime_setting(actor[2], "ai_paused", "1")
        self._invalidate_policy(actor[2])
        await message.answer("AI 审核已暂停；暂停期间普通成员消息将保留。")

    async def ai_resume(self, message: Message) -> None:
        actor = await self._private_ai_admin(message)
        if actor is None:
            return
        await self.storage.set_runtime_setting(actor[2], "ai_paused", "0")
        self._invalidate_policy(actor[2])
        await message.answer("AI 审核已恢复。")

    async def _private_ai_admin(self, message: Message) -> tuple[int, AdminRights, int] | None:
        actor = await self._group_admin(message)
        if actor is None:
            return None
        if message.chat.type != ChatType.PRIVATE:
            await message.answer("此 AI 管理命令仅可在机器人私聊中使用。")
            return None
        return actor

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

    @staticmethod
    def _member_activity_stamp(activity: MemberActivity) -> str:
        return max(
            activity.first_observed_at,
            activity.last_message_at or activity.first_observed_at,
            activity.last_membership_at or activity.first_observed_at,
        )

    def _prune_member_cleanup_plans(self) -> None:
        now = time.monotonic()
        for key, plan in tuple(self._member_cleanup_plans.items()):
            if plan.expires_at <= now:
                self._member_cleanup_plans.pop(key, None)

    def _new_member_confirmation_code(self, actor_id: int) -> str:
        while True:
            code = secrets.token_hex(3).upper()
            if (actor_id, code) not in self._member_cleanup_plans:
                return code

    @staticmethod
    def _member_reason_text(candidate: MemberCandidate) -> str:
        labels = {
            REASON_DELETED: "已注销",
            REASON_OFFLINE: "精确离线超过 30 天",
            REASON_LAST_MONTH: "上月上线（模糊）",
            REASON_SILENT: "30 天无发言",
        }
        return "、".join(labels[reason] for reason in candidate.reasons)

    async def _delete_members(self, message: Message, arguments: tuple[str, ...]) -> None:
        actor = await self._private_member_admin(message, restrict=True)
        if actor is None:
            return
        self._prune_member_cleanup_plans()
        if len(arguments) == 2 and arguments[1].casefold() == "confirm":
            code = arguments[0].upper()
            plan = self._member_cleanup_plans.pop((actor[0], code), None)
            if plan is None or plan.expires_at <= time.monotonic():
                await message.answer("确认码无效或已过期；请重新生成清理计划。")
                return
            await self._execute_member_cleanup(message, plan)
            return
        if arguments:
            await message.answer(
                "用法：<code>/del members</code>\n确认：<code>/del members 确认码 confirm</code>"
            )
            return
        preview_started_at = datetime.now(UTC).replace(microsecond=0)
        cutoff = preview_started_at - timedelta(days=MEMBER_CLEANUP_DAYS)
        try:
            result = await self.member_scanner.scan(actor[2], cutoff=cutoff)
        except MemberScanError as exc:
            await message.answer(f"成员扫描失败：{html.escape(str(exc))}")
            return
        eligible: list[MemberCandidate] = []
        for candidate in result.candidates:
            is_admin, is_trusted = await asyncio.gather(
                self.admins.cached_is_admin(actor[2], candidate.user_id),
                self.storage.is_trusted(actor[2], candidate.user_id),
            )
            if is_admin is not False or is_trusted:
                continue
            eligible.append(candidate)
        if not eligible:
            await message.answer(
                f"成员扫描完成：共核验 {result.participants_scanned} 人，没有清理候选。"
                + (
                    "\n群历史读取不完整，本次未使用“30 天无发言”条件。"
                    if not result.history_complete
                    else ""
                )
            )
            return
        selected = tuple(eligible[:INACTIVE_CLEANUP_LIMIT])
        for key in tuple(self._member_cleanup_plans):
            if key[0] == actor[0]:
                self._member_cleanup_plans.pop(key, None)
        code = self._new_member_confirmation_code(actor[0])
        plan = PendingMemberCleanup(
            actor_id=actor[0],
            chat_id=actor[2],
            cutoff=cutoff,
            created_at=preview_started_at,
            members=selected,
            expires_at=time.monotonic() + INACTIVE_CONFIRM_TTL_SECONDS,
        )
        self._member_cleanup_plans[(actor[0], code)] = plan
        lines = [
            f"<code>{candidate.user_id}</code>｜{self._member_reason_text(candidate)}"
            for candidate in selected
        ]
        await message.answer(
            f"<b>成员清理预览（{len(selected)}/{len(eligible)}）</b>\n"
            + "\n".join(lines)
            + (
                "\n\n群历史读取不完整，本次未使用“30 天无发言”条件。"
                if not result.history_complete
                else ""
            )
            + "\n\n清理前会再次核验管理员、机器人、可信用户、成员状态和新增群活动。"
            + (
                "\n候选超过 50 人，本次只处理排序最前的 50 人。"
                if len(eligible) > INACTIVE_CLEANUP_LIMIT
                else ""
            )
            + f"\n10 分钟内发送：<code>/del members {code} confirm</code>"
        )

    @staticmethod
    def _member_candidate_still_matches(
        candidate: MemberCandidate,
        *,
        current_status_reason: str | None,
        current_deleted: bool,
        recently_active: bool,
    ) -> bool:
        if recently_active:
            return False
        status_reasons = {REASON_OFFLINE, REASON_LAST_MONTH}
        deleted_match = REASON_DELETED in candidate.reasons and current_deleted
        status_match = bool(status_reasons.intersection(candidate.reasons)) and (
            current_status_reason in status_reasons
        )
        silent_match = REASON_SILENT in candidate.reasons
        return deleted_match or status_match or silent_match

    async def _execute_member_cleanup(
        self,
        message: Message,
        plan: PendingMemberCleanup,
    ) -> None:
        try:
            current_states, active_since_preview = await self.member_scanner.current_members(
                plan.chat_id,
                cutoff=plan.cutoff,
                activity_since=plan.created_at,
                user_ids=frozenset(candidate.user_id for candidate in plan.members),
            )
        except MemberScanError as exc:
            await message.answer(f"成员复核失败，未执行清理：{html.escape(str(exc))}")
            return
        cleaned = 0
        skipped = 0
        failed = 0
        unban_failed = 0
        not_processed = 0
        admin_statuses = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
        absent_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
        for index, candidate in enumerate(plan.members):
            user_id = candidate.user_id
            async with self._member_action_lock(plan.chat_id, user_id):
                activity, is_trusted = await asyncio.gather(
                    self.storage.member_activity(plan.chat_id, user_id),
                    self.storage.is_trusted(plan.chat_id, user_id),
                )
                try:
                    member = await telegram_call_with_bounded_retry(
                        lambda target_id=user_id: self.bot.get_chat_member(plan.chat_id, target_id)
                    )
                except TelegramRetryAfter:
                    logger.warning(
                        "清理前核验成员触发长时间限流 chat_id=%s user_id=%s",
                        plan.chat_id,
                        user_id,
                    )
                    failed += 1
                    not_processed = len(plan.members) - index - 1
                    break
                except TelegramAPIError:
                    logger.exception(
                        "清理前核验成员失败 chat_id=%s user_id=%s",
                        plan.chat_id,
                        user_id,
                    )
                    failed += 1
                    continue
                state = current_states.get(user_id)
                recently_active = user_id in active_since_preview or (
                    activity is not None
                    and self._member_activity_stamp(activity)
                    >= plan.created_at.isoformat(timespec="seconds")
                )
                if (
                    state is None
                    or state.administrator
                    or state.bot
                    or (activity is not None and activity.last_status in {"left", "kicked"})
                    or user_id in self.settings.authorized_admin_ids
                    or is_trusted
                    or member.status in admin_statuses | absent_statuses
                    or getattr(getattr(member, "user", None), "is_bot", False)
                    or not self._member_candidate_still_matches(
                        candidate,
                        current_status_reason=state.status_reason,
                        current_deleted=state.deleted,
                        recently_active=recently_active,
                    )
                ):
                    skipped += 1
                    continue
                try:
                    await telegram_call_with_bounded_retry(
                        lambda target_id=user_id: self.bot.ban_chat_member(
                            plan.chat_id,
                            target_id,
                            until_date=datetime.now(UTC)
                            + timedelta(seconds=INACTIVE_KICK_FALLBACK_SECONDS),
                            revoke_messages=False,
                        )
                    )
                except TelegramRetryAfter:
                    logger.warning(
                        "长期未发言成员移出触发长时间限流 chat_id=%s user_id=%s",
                        plan.chat_id,
                        user_id,
                    )
                    failed += 1
                    not_processed = len(plan.members) - index - 1
                    break
                except TelegramAPIError:
                    logger.exception(
                        "长期未发言成员移出失败 chat_id=%s user_id=%s",
                        plan.chat_id,
                        user_id,
                    )
                    failed += 1
                    continue
                try:
                    await telegram_call_with_bounded_retry(
                        lambda target_id=user_id: self.bot.unban_chat_member(
                            plan.chat_id,
                            target_id,
                            only_if_banned=True,
                        )
                    )
                except TelegramAPIError:
                    logger.exception(
                        "长期未发言成员移出后解除封禁失败 chat_id=%s user_id=%s",
                        plan.chat_id,
                        user_id,
                    )
                    await self.reporter.record(
                        chat_id=plan.chat_id,
                        action="cleanup_inactive_unban_failed",
                        actor_id=plan.actor_id,
                        target_id=user_id,
                    )
                    failed += 1
                    unban_failed += 1
                    not_processed = len(plan.members) - index - 1
                    break
                await self.reporter.record(
                    chat_id=plan.chat_id,
                    action="cleanup_inactive",
                    actor_id=plan.actor_id,
                    target_id=user_id,
                )
                cleaned += 1
        await message.answer(
            f"清理完成：已移出 {cleaned} 人，跳过 {skipped} 人，失败 {failed} 人，"
            f"未继续处理 {not_processed} 人。\n"
            + (
                f"其中 {unban_failed} 人解除封禁失败；临时封禁最多 5 分钟后自动失效，"
                "请通过 /recent 查看目标并核查。\n"
                if unban_failed
                else ""
            )
            + "本次候选依据为 Telegram 账号状态或最近 30 天群内发言。"
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
            observed_value = message.edit_date or message.date
            observed_at = (
                observed_value
                if isinstance(observed_value, datetime)
                else datetime.fromtimestamp(observed_value, UTC)
            )
            try:
                async with self._member_action_lock(message.chat.id, user_id):
                    await self.storage.record_member_message(
                        message.chat.id,
                        user_id,
                        observed_at=observed_at.astimezone(UTC).isoformat(timespec="seconds"),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "成员活跃度写入失败 chat_id=%s user_id=%s",
                    message.chat.id,
                    user_id,
                )
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
        use_high_reasoning = (
            suspicion.is_suspicious or flood_violation is not None or has_media_preview
        )
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
                self._adaptive_review(
                    text=text,
                    image_data_url=image_data_url,
                    safety_identifier=f"{message.chat.id}:{safety_target}",
                    risk_signals=normalized_risk_signals,
                    linked_hosts=linked_hosts,
                    use_high_reasoning=use_high_reasoning,
                ),
                timeout=self.settings.ai_timeout_seconds,
            )
        except (AIReviewError, AiohttpClientError, asyncio.TimeoutError) as exc:
            processing = await processing_task if processing_task is not None else None
            await self._handle_ai_failure(
                message,
                target_id=target_id,
                processing=processing,
                error_type=type(exc).__name__,
                media_latency_ms=media_latency_ms,
                reasoning_effort=(
                    self.settings.ai_suspicious_reasoning_effort
                    if use_high_reasoning
                    else self.settings.ai_normal_reasoning_effort
                ),
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

    async def _adaptive_review(
        self,
        *,
        text: str,
        image_data_url: str | None,
        safety_identifier: str,
        risk_signals: tuple[str, ...],
        linked_hosts: tuple[str, ...],
        use_high_reasoning: bool,
    ):
        if self.ai_reviewer is None:
            raise RuntimeError("AI reviewer is unavailable")
        initial_effort = (
            self.settings.ai_suspicious_reasoning_effort
            if use_high_reasoning
            else self.settings.ai_normal_reasoning_effort
        )
        result = await self.ai_reviewer.review(
            text=text,
            image_data_url=image_data_url,
            safety_identifier=safety_identifier,
            risk_signals=risk_signals,
            linked_hosts=linked_hosts,
            reasoning_effort=initial_effort,
        )
        if initial_effort != "low" or (
            not result.is_ad and result.confidence >= self.settings.ai_normal_accept_confidence
        ):
            return result

        confirmed = await self.ai_reviewer.review(
            text=text,
            image_data_url=image_data_url,
            safety_identifier=safety_identifier,
            risk_signals=risk_signals,
            linked_hosts=linked_hosts,
            reasoning_effort=self.settings.ai_suspicious_reasoning_effort,
        )
        updates: dict[str, object] = {
            "latency_ms": result.latency_ms + confirmed.latency_ms,
        }
        for field_name in (
            "queue_latency_ms",
            "request_latency_ms",
            "attempt_count",
        ):
            if hasattr(result, field_name) and hasattr(confirmed, field_name):
                updates[field_name] = int(getattr(result, field_name)) + int(
                    getattr(confirmed, field_name)
                )
        if hasattr(confirmed, "review_path"):
            updates["review_path"] = "low_to_high"
        return replace(confirmed, **updates)

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
        if self.is_allowed_chat(event.chat.id) and not user.is_bot:
            try:
                async with self._member_action_lock(event.chat.id, user.id):
                    await self.storage.record_member_status(
                        event.chat.id,
                        user.id,
                        status,
                        observed_at=event.date.astimezone(UTC).isoformat(timespec="seconds"),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "成员状态写入失败 chat_id=%s user_id=%s",
                    event.chat.id,
                    user.id,
                )
        key = (event.chat.id, user.id)
        if status == ChatMemberStatus.KICKED.value:
            self._remember_auto_banned(key)
        else:
            self._forget_auto_banned(key)
