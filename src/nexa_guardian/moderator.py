from __future__ import annotations

import asyncio
import base64
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
from aiogram.filters import Command, CommandStart
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
)
from nexa_guardian.storage import AIStats, ChatStats, LogOutboxRecord, MemberActivity, Storage

logger = logging.getLogger(__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
AUTO_BAN_MEMORY_SECONDS = 60 * 60
AUTO_BAN_MEMORY_MAX = 10_000
AI_CANDIDATE_PROMOTION_HITS = 3
INACTIVE_MIN_DAYS = 30
INACTIVE_MAX_DAYS = 365
INACTIVE_CLEANUP_LIMIT = 50
INACTIVE_CONFIRM_TTL_SECONDS = 10 * 60
INACTIVE_KICK_FALLBACK_SECONDS = 5 * 60
INACTIVE_RETRY_AFTER_MAX_SECONDS = 15
BOT_PROFILE_NAME = "Nexa 群管助手"
BOT_PROFILE_SHORT_DESCRIPTION = (
    "Nexa 官方群管理审核员：GPT-5.6 Sol 智能识别广告与刷屏，自动删除并警告；"
    "第 3 次直接封禁，支持管理员筛选长期未发言成员。"
)
BOT_PROFILE_DESCRIPTION = """Nexa Guardian 是 @NexaStoreGroup 的官方群管理与安全审核员。

• GPT-5.6 Sol 审核文字、图片及视频封面
• 自动清理广告、诈骗、恶意推广与刷屏；第 3 次警告直接封禁
• 普通聊天静默处理；仅可疑消息显示审核状态
• 持续学习高置信广告特征，提升后续拦截效率
• 支持警告、禁言、解禁、移出、封禁、可信用户与域名白名单
• 唯一授权管理员可私聊筛选并二次确认清理长期未发言的已观察成员
• 群内机器人回复约 30 秒后清理；管理员私聊回复永久保留
• 不保存群消息原文、图片或模型推理

成员筛选依据机器人观察到的活动，不代表 Telegram 最后上线时间。发送 /help 查看管理命令。

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
class PendingInactiveCleanup:
    actor_id: int
    chat_id: int
    days: int
    cutoff: str
    members: tuple[tuple[int, str], ...]
    expires_at: float


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

HELP_TEXT = """<b>Nexa Guardian 群管命令</b>

自动功能：GPT-5.6 Sol 广告审核、AI 特征学习、累计 3 次警告自动封禁。

群内可回复成员消息；私聊机器人时，把数字用户 ID 放在命令后：
<code>/warn</code>　警告
<code>/mute [1m|2h|1d]</code>　禁言
<code>/unmute</code>　解除禁言
<code>/ban</code>　封禁并删除其历史消息
<code>/unban 用户ID</code>　解除封禁
<code>/kick</code>　移出但允许重新加入
<code>/del</code>　群内删除被回复的消息
<code>/warnings</code> / <code>/clearwarns</code>

规则管理：
<code>/trust</code> / <code>/untrust</code>　可信用户
<code>/addomain example.com</code> / <code>/deldomain example.com</code>
<code>/domains</code>　查看域名白名单
<code>/words</code>　查看 AI 已学习特征
<code>/delword 编号或完整特征</code>　停用误学特征
<code>/aicandidates</code>　查看候选或拒绝特征
<code>/aipause</code> / <code>/airesume</code>　暂停或恢复 AI

检查：<code>/status</code> <code>/settings</code> <code>/stats</code>
<code>/recent</code> <code>/aistats</code> <code>/airecent</code> <code>/chatid</code>
<code>/inactive [30-365]</code>　私聊预览长期未发言成员
<code>/cleanupinactive [30-365]</code>　私聊生成二次确认清理计划

私聊示例：<code>/mute 123456789 2h</code>
私聊仅唯一授权管理员可用，机器人私聊回复永久保留；群内回复约 30 秒后清理。
成员筛选只依据机器人上线后观察到的群内活动，不代表 Telegram 最后上线时间。"""


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
        self._inactive_cleanup_plans: dict[tuple[int, str], PendingInactiveCleanup] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        # Private updates from anyone except the single configured administrator
        # are silently consumed before any command handler can respond.
        self.router.message.register(
            self._ignore_unauthorized_private,
            self._unauthorized_private_message,
        )
        # Member-authored command-looking messages are still untrusted content.
        # Register this guard before Telegram command handlers so only verified
        # admins (and other moderation exemptions) can reach the command path.
        self.router.message.register(
            self.moderate_message,
            self._ordinary_member_command,
        )
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.help, Command("help"))
        self.router.message.register(self.chat_id, Command("chatid"))
        self.router.message.register(self.status, Command("status"))
        self.router.message.register(self.settings_command, Command("settings"))
        self.router.message.register(self.stats, Command("stats"))
        self.router.message.register(self.recent, Command("recent"))
        self.router.message.register(self.warn, Command("warn"))
        self.router.message.register(self.mute, Command("mute"))
        self.router.message.register(self.unmute, Command("unmute"))
        self.router.message.register(self.ban, Command("ban"))
        self.router.message.register(self.unban, Command("unban"))
        self.router.message.register(self.kick, Command("kick"))
        self.router.message.register(self.delete_replied, Command("del"))
        self.router.message.register(self.warnings, Command("warnings"))
        self.router.message.register(self.clear_warnings, Command("clearwarns"))
        self.router.message.register(self.trust, Command("trust"))
        self.router.message.register(self.untrust, Command("untrust"))
        self.router.message.register(self.add_domain, Command("addomain"))
        self.router.message.register(self.delete_domain, Command("deldomain"))
        self.router.message.register(self.list_domains, Command("domains"))
        self.router.message.register(self.add_word, Command("addword"))
        self.router.message.register(self.delete_word, Command("delword"))
        self.router.message.register(self.list_words, Command("words"))
        self.router.message.register(self.ai_candidates, Command("aicandidates"))
        self.router.message.register(self.ai_stats, Command("aistats"))
        self.router.message.register(self.ai_recent, Command("airecent"))
        self.router.message.register(self.ai_pause, Command("aipause"))
        self.router.message.register(self.ai_resume, Command("airesume"))
        self.router.message.register(self.inactive_members, Command("inactive"))
        self.router.message.register(self.cleanup_inactive, Command("cleanupinactive"))
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
            if await self.storage.is_trusted(message.chat.id, user_id):
                return False
            return True
        return message.sender_chat is not None

    async def set_commands(self) -> None:
        commands = [
            BotCommand(command="help", description="查看群内和私聊管理命令"),
            BotCommand(command="status", description="检查机器人权限和状态"),
            BotCommand(command="settings", description="查看当前群管规则"),
            BotCommand(command="stats", description="查看今日、近7天和累计统计"),
            BotCommand(command="recent", description="私聊查看最近管理操作"),
            BotCommand(command="warn", description="警告：/warn 用户ID"),
            BotCommand(command="mute", description="禁言：/mute 用户ID 2h"),
            BotCommand(command="unmute", description="解禁：/unmute 用户ID"),
            BotCommand(command="ban", description="封禁：/ban 用户ID"),
            BotCommand(command="unban", description="解除封禁：/unban 用户ID"),
            BotCommand(command="kick", description="移出：/kick 用户ID"),
            BotCommand(command="del", description="删除回复的群消息"),
            BotCommand(command="warnings", description="查询用户警告次数"),
            BotCommand(command="clearwarns", description="清除用户警告"),
            BotCommand(command="trust", description="添加可信用户"),
            BotCommand(command="untrust", description="移除可信用户"),
            BotCommand(command="delword", description="停用误学的 AI 特征"),
            BotCommand(command="domains", description="查看域名白名单"),
            BotCommand(command="addomain", description="添加域名白名单"),
            BotCommand(command="deldomain", description="移除域名白名单"),
            BotCommand(command="words", description="查看 AI 已学习特征"),
            BotCommand(command="aicandidates", description="查看 AI 候选特征"),
            BotCommand(command="aistats", description="查看 AI 审核统计"),
            BotCommand(command="airecent", description="查看最近 AI 判断"),
            BotCommand(command="aipause", description="紧急暂停 AI 审核"),
            BotCommand(command="airesume", description="恢复 AI 审核"),
            BotCommand(command="inactive", description="私聊预览长期未发言成员"),
            BotCommand(command="cleanupinactive", description="私聊二次确认清理成员"),
            BotCommand(command="chatid", description="查看目标群 ID"),
        ]
        for language_code in (None, "zh"):
            await self.bot.set_my_commands(commands, language_code=language_code)
            await self.bot.set_my_name(BOT_PROFILE_NAME, language_code=language_code)
            await self.bot.set_my_short_description(
                BOT_PROFILE_SHORT_DESCRIPTION,
                language_code=language_code,
            )
            await self.bot.set_my_description(
                BOT_PROFILE_DESCRIPTION,
                language_code=language_code,
            )

    async def startup(self) -> None:
        await self.reporter.start()

    async def shutdown(self) -> None:
        await self.reporter.shutdown()
        close_reviewer = getattr(self.ai_reviewer, "close", None)
        if close_reviewer is not None:
            await close_reviewer()

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
            except TelegramAPIError:
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
        except TelegramAPIError:
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
        await message.answer(
            f"<b>运行状态</b>\n"
            f"机器人：@{html.escape(me.username or str(me.id))}\n"
            f"目标群：<code>{chat_id}</code>\n"
            f"群白名单：{'是' if allowed else '否'}\n"
            f"自动群管：{'启用' if allowed else '停用'}\n"
            f"模式：{mode}\n"
            f"机器人管理员：{'是' if is_admin else '否'}\n"
            f"删除消息权限：{'有' if can_delete else '缺少'}\n"
            f"限制成员权限：{'有' if can_restrict else '缺少'}"
        )

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
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        chat_id = actor[2]
        target_message = message.reply_to_message
        if target_message is not None and message.chat.type in GROUP_TYPES:
            target_message_id = target_message.message_id
            target_user_id = target_message.from_user.id if target_message.from_user else None
        else:
            arguments = command_arguments(message)
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
        await message.answer("AI 审核已暂停；暂停期间普通成员消息将保留。")

    async def ai_resume(self, message: Message) -> None:
        actor = await self._private_ai_admin(message)
        if actor is None:
            return
        await self.storage.set_runtime_setting(actor[2], "ai_paused", "0")
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
    def _inactive_days(arguments: tuple[str, ...]) -> int | None:
        if len(arguments) > 1:
            return None
        if not arguments:
            return INACTIVE_MIN_DAYS
        try:
            days = int(arguments[0])
        except ValueError:
            return None
        return days if INACTIVE_MIN_DAYS <= days <= INACTIVE_MAX_DAYS else None

    @staticmethod
    def _member_activity_stamp(activity: MemberActivity) -> str:
        return max(
            activity.first_observed_at,
            activity.last_message_at or activity.first_observed_at,
            activity.last_membership_at or activity.first_observed_at,
        )

    async def _inactive_candidates(
        self,
        chat_id: int,
        *,
        cutoff: str,
    ) -> tuple[MemberActivity, ...]:
        observed = await self.storage.inactive_member_candidates(
            chat_id,
            cutoff=cutoff,
            limit=INACTIVE_CLEANUP_LIMIT * 5,
        )
        eligible: list[MemberActivity] = []
        for activity in observed:
            if activity.user_id in self.settings.authorized_admin_ids:
                continue
            is_admin, is_trusted = await asyncio.gather(
                self.admins.cached_is_admin(chat_id, activity.user_id),
                self.storage.is_trusted(chat_id, activity.user_id),
            )
            # An unknown administrator state is never safe to clean.
            if is_admin is not False or is_trusted:
                continue
            eligible.append(activity)
            if len(eligible) >= INACTIVE_CLEANUP_LIMIT:
                break
        return tuple(eligible)

    async def inactive_members(self, message: Message) -> None:
        actor = await self._private_member_admin(message, restrict=False)
        if actor is None:
            return
        arguments = command_arguments(message)
        days = self._inactive_days(arguments)
        if days is None:
            await message.answer("用法：<code>/inactive [30-365]</code>")
            return
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        candidates = await self._inactive_candidates(actor[2], cutoff=cutoff)
        if not candidates:
            await message.answer(
                f"<b>{days} 天未发言候选（0）</b>\n"
                "当前没有满足条件的已观察成员。\n\n"
                "群内活动不代表 Telegram 最后上线；机器人无法读取用户最后上线时间，"
                "也无法枚举接入前的历史成员。"
            )
            return
        lines = [
            f"<code>{item.user_id}</code>｜最后群内活动 "
            f"{action_time(self._member_activity_stamp(item))}"
            for item in candidates
        ]
        await message.answer(
            f"<b>{days} 天未发言候选（{len(candidates)}）</b>\n"
            + "\n".join(lines)
            + "\n\n群内活动不代表 Telegram 最后上线；清理前还会重新核验身份和最新活动。"
        )

    def _prune_inactive_cleanup_plans(self) -> None:
        now = time.monotonic()
        for key, plan in tuple(self._inactive_cleanup_plans.items()):
            if plan.expires_at <= now:
                self._inactive_cleanup_plans.pop(key, None)

    def _new_inactive_confirmation_code(self, actor_id: int) -> str:
        while True:
            code = secrets.token_hex(3).upper()
            if (actor_id, code) not in self._inactive_cleanup_plans:
                return code

    async def cleanup_inactive(self, message: Message) -> None:
        actor = await self._private_member_admin(message, restrict=True)
        if actor is None:
            return
        self._prune_inactive_cleanup_plans()
        arguments = command_arguments(message)
        if len(arguments) == 2 and arguments[1].casefold() == "confirm":
            code = arguments[0].upper()
            plan = self._inactive_cleanup_plans.pop((actor[0], code), None)
            if plan is None or plan.expires_at <= time.monotonic():
                await message.answer("确认码无效或已过期；请重新生成清理计划。")
                return
            await self._execute_inactive_cleanup(message, plan)
            return

        days = self._inactive_days(arguments)
        if days is None:
            await message.answer(
                "用法：<code>/cleanupinactive [30-365]</code>\n"
                "确认：<code>/cleanupinactive 确认码 confirm</code>"
            )
            return
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        candidates = await self._inactive_candidates(actor[2], cutoff=cutoff)
        if not candidates:
            await message.answer(
                f"没有可清理的 {days} 天未发言成员。\n群内活动不代表 Telegram 最后上线。"
            )
            return
        for key in tuple(self._inactive_cleanup_plans):
            if key[0] == actor[0]:
                self._inactive_cleanup_plans.pop(key, None)
        code = self._new_inactive_confirmation_code(actor[0])
        plan = PendingInactiveCleanup(
            actor_id=actor[0],
            chat_id=actor[2],
            days=days,
            cutoff=cutoff,
            members=tuple((item.user_id, self._member_activity_stamp(item)) for item in candidates),
            expires_at=time.monotonic() + INACTIVE_CONFIRM_TTL_SECONDS,
        )
        self._inactive_cleanup_plans[(actor[0], code)] = plan
        await message.answer(
            f"已生成清理计划：{len(plan.members)} 人，阈值 {days} 天。\n"
            "机器人会再次核验管理员、机器人、可信用户、成员状态和最新群内活动。\n"
            "群内活动不代表 Telegram 最后上线。\n"
            f"10 分钟内发送：<code>/cleanupinactive {code} confirm</code>"
        )

    async def _execute_inactive_cleanup(
        self,
        message: Message,
        plan: PendingInactiveCleanup,
    ) -> None:
        cleaned = 0
        skipped = 0
        failed = 0
        unban_failed = 0
        not_processed = 0
        admin_statuses = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
        absent_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
        for index, (user_id, expected_activity) in enumerate(plan.members):
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
                if (
                    activity is None
                    or self._member_activity_stamp(activity) != expected_activity
                    or self._member_activity_stamp(activity) >= plan.cutoff
                    or activity.last_status in {"left", "kicked"}
                    or user_id in self.settings.authorized_admin_ids
                    or is_trusted
                    or member.status in admin_statuses | absent_statuses
                    or getattr(getattr(member, "user", None), "is_bot", False)
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
            + "本次依据为机器人观察到的群内活动，不是 Telegram 最后上线时间。"
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
            if await self.storage.is_trusted(message.chat.id, user_id):
                return
        elif message.sender_chat is None:
            return

        text = message.text or message.caption or ""
        entities = message.entities if message.text is not None else message.caption_entities
        entity_urls = extract_entity_urls(text, entities)

        if not self.settings.ai_enabled or self.ai_reviewer is None:
            await self._moderate_legacy(message, user_id, text, entity_urls)
            return
        if await self.storage.runtime_setting(message.chat.id, "ai_paused") == "1":
            return

        active_signatures, stored_allowed_domains = await asyncio.gather(
            self.storage.active_ai_signatures(message.chat.id),
            self.storage.allowed_domains(message.chat.id),
        )
        suspicion = suspicious_precheck(
            text,
            entity_urls=entity_urls,
            is_forwarded=message.forward_origin is not None,
            active_signatures=active_signatures,
            allowed_domains=set(self.settings.allowed_domains) | stored_allowed_domains,
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
        is_admin = event.new_chat_member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        }
        self.admins.update_member(event.chat.id, user.id, is_admin=is_admin)
        if self.is_allowed_chat(event.chat.id) and not user.is_bot:
            try:
                async with self._member_action_lock(event.chat.id, user.id):
                    await self.storage.record_member_status(
                        event.chat.id,
                        user.id,
                        event.new_chat_member.status.value,
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
        if event.new_chat_member.status == ChatMemberStatus.KICKED:
            self._remember_auto_banned(key)
        else:
            self._forget_auto_banned(key)
