from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    ChatMemberAdministrator,
    ChatMemberOwner,
    ChatMemberUpdated,
    ChatPermissions,
    Message,
    User,
)

from nexa_guardian.admin import AdminRights, AdminService
from nexa_guardian.commands import (
    batch_items,
    command_arguments,
    duration_from,
    resolve_target,
)
from nexa_guardian.config import Settings
from nexa_guardian.detection import (
    AdDetector,
    DetectionInput,
    FloodGuard,
    Violation,
    extract_entity_urls,
    human_duration,
    normalize_domain,
)
from nexa_guardian.storage import ChatStats, Storage

logger = logging.getLogger(__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
ACTION_LABELS = {
    "delete": "删除消息",
    "warn": "警告",
    "mute": "禁言",
    "auto_mute": "自动禁言",
    "unmute": "解除禁言",
    "ban": "封禁",
    "unban": "解除封禁",
    "kick": "移出群组",
    "kick_unban_failed": "移出后解封失败",
    "clear_warnings": "清除警告",
    "trust": "添加可信用户",
    "untrust": "移除可信用户",
    "add_words": "添加拦截词",
    "delete_words": "移除拦截词",
    "add_domain": "添加域名白名单",
    "delete_domain": "移除域名白名单",
    "dry_run_hit": "观察模式命中",
    "delete_failed": "删除失败",
}

HELP_TEXT = """<b>Nexa Guardian 群管命令</b>

自动功能：外链/关键词广告删除、刷屏检测、累计警告自动禁言。

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
<code>/addword 词1，词2</code> / <code>/delword 词1，词2</code>
<code>/words</code>　查看拦截词

检查：<code>/status</code> <code>/settings</code> <code>/stats</code>
<code>/recent</code> <code>/chatid</code>

私聊示例：<code>/mute 123456789 2h</code>
私聊仅授权管理员可用；当前只有一个授权群时会自动选择该群。"""


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
        )
        logger.info(
            "moderation action=%s chat_id=%s actor_id=%s target_id=%s message_id=%s",
            action,
            chat_id,
            actor_id,
            target_id,
            message_id,
        )
        if self.log_chat_id is None:
            return
        text = (
            f"<b>群管日志</b>\n"
            f"动作：<code>{html.escape(action)}</code>\n"
            f"群：<code>{chat_id}</code>\n"
            f"目标：<code>{target_id or '-'}</code>\n"
            f"操作者：<code>{actor_id or '自动'}</code>"
        )
        try:
            await self.bot.send_message(self.log_chat_id, text)
        except TelegramAPIError:
            logger.exception("发送群管日志失败 log_chat_id=%s", self.log_chat_id)


class ModerationController:
    def __init__(self, bot: Bot, settings: Settings, storage: Storage) -> None:
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
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._register_handlers()

    def _register_handlers(self) -> None:
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
        self.router.message.register(self.moderate_message)
        self.router.chat_member.register(self.chat_member_changed)

    async def set_commands(self) -> None:
        await self.bot.set_my_commands(
            [
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
                BotCommand(command="addword", description="添加拦截词，可逗号分隔"),
                BotCommand(command="delword", description="移除拦截词，可逗号分隔"),
                BotCommand(command="domains", description="查看域名白名单"),
                BotCommand(command="addomain", description="添加域名白名单"),
                BotCommand(command="deldomain", description="移除域名白名单"),
                BotCommand(command="words", description="查看广告拦截词"),
                BotCommand(command="chatid", description="查看目标群 ID"),
            ]
        )

    async def shutdown(self) -> None:
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

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
            if (
                not self.settings.authorized_admin_ids
                or message.from_user.id not in self.settings.authorized_admin_ids
            ):
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
            sent = await message.answer(text)
        except TelegramAPIError:
            logger.exception("发送群管提示失败 chat_id=%s", message.chat.id)
            return
        if self.settings.notice_ttl_seconds <= 0:
            return
        task = asyncio.create_task(
            self._delete_later(sent.chat.id, sent.message_id, self.settings.notice_ttl_seconds)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _delete_later(self, chat_id: int, message_id: int, delay: int) -> None:
        await asyncio.sleep(delay)
        try:
            await self.bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            logger.debug("临时提示删除失败 chat_id=%s message_id=%s", chat_id, message_id)

    async def _send_chunked_list(
        self, message: Message, *, title: str, items: list[str], max_length: int = 3500
    ) -> None:
        if not items:
            await message.answer(f"<b>{html.escape(title)}（0）</b>\n（空）")
            return
        bodies: list[str] = []
        current: list[str] = []
        current_length = 0
        for item in items:
            line = f"• <code>{html.escape(item)}</code>"
            if current and current_length + len(line) + 1 > max_length:
                bodies.append("\n".join(current))
                current = []
                current_length = 0
            current.append(line)
            current_length += len(line) + 1
        if current:
            bodies.append("\n".join(current))
        for index, body in enumerate(bodies, start=1):
            page = f" {index}/{len(bodies)}" if len(bodies) > 1 else ""
            await message.answer(
                f"<b>{html.escape(title)}（{len(items)}）{page}</b>\n{body}"
            )

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
        domains = set(self.settings.allowed_domains) | await self.storage.allowed_domains(
            chat_id
        )
        words = set(self.settings.blocked_words) | await self.storage.blocked_words(chat_id)
        await message.answer(
            f"<b>当前群管规则</b>\n"
            f"外链过滤：{'开' if self.settings.block_external_links else '关'}\n"
            f"转发过滤：{'开' if self.settings.block_forwards else '关'}\n"
            f"@账号过滤：{'开' if self.settings.block_channel_mentions else '关'}\n"
            f"警告阈值：{self.settings.warn_limit}\n"
            f"自动禁言：{human_duration(self.settings.default_mute_seconds)}\n"
            f"域名白名单：{len(domains)} 条\n"
            f"拦截词：{len(words)} 条\n"
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
        count = await self.storage.add_warning(chat_id, user_id)
        muted = False
        if count >= self.settings.warn_limit:
            try:
                await self._mute_member(chat_id, user_id, self.settings.default_mute_seconds)
                await self.storage.clear_warnings(chat_id, user_id)
                await self.reporter.record(
                    chat_id=chat_id,
                    action="auto_mute",
                    actor_id=actor_id,
                    target_id=user_id,
                )
                muted = True
            except TelegramAPIError as exc:
                await message.answer(f"警告已记录，但自动禁言失败：{html.escape(str(exc))}")
        await self.reporter.record(
            chat_id=chat_id,
            action="warn",
            actor_id=actor_id,
            target_id=user_id,
        )
        suffix = (
            f"；达到阈值，已禁言 {human_duration(self.settings.default_mute_seconds)}"
            if muted
            else f"（{count}/{self.settings.warn_limit}）"
        )
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
        try:
            await self.bot.ban_chat_member(chat_id, user_id, revoke_messages=True)
        except TelegramAPIError as exc:
            await message.answer(f"封禁失败：{html.escape(str(exc))}")
            return
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
        try:
            await self.bot.unban_chat_member(chat_id, target.user_id, only_if_banned=True)
        except TelegramAPIError as exc:
            await message.answer(f"解除封禁失败：{html.escape(str(exc))}")
            return
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
        try:
            await self.bot.ban_chat_member(chat_id, user_id, revoke_messages=False)
            banned = True
            await self.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
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
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        chat_id = actor[2]
        try:
            words = batch_items(message)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        if not words:
            await message.answer("用法：<code>/addword 词语1，词语2</code>")
            return
        invalid = [word for word in words if len(word) < 2 or len(word) > 64]
        if invalid:
            await message.answer("所有拦截词长度必须为 2–64 个字符，本次未添加。")
            return
        await self.storage.add_blocked_words(chat_id, words, actor[0])
        await self.reporter.record(
            chat_id=chat_id,
            action="add_words",
            actor_id=actor[0],
        )
        body = "、".join(f"<code>{html.escape(word)}</code>" for word in words)
        await message.answer(f"已添加 {len(words)} 条拦截词：{body}")

    async def delete_word(self, message: Message) -> None:
        actor = await self._group_admin(message, delete=True)
        if actor is None:
            return
        chat_id = actor[2]
        try:
            words = batch_items(message)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        if not words:
            await message.answer("用法：<code>/delword 词语1，词语2</code>")
            return
        global_words = set(self.settings.blocked_words)
        protected = [word for word in words if word in global_words]
        if protected:
            await message.answer("包含环境变量中的全局拦截词，本次未删除。")
            return
        await self.storage.remove_blocked_words(chat_id, words)
        await self.reporter.record(
            chat_id=chat_id,
            action="delete_words",
            actor_id=actor[0],
        )
        body = "、".join(f"<code>{html.escape(word)}</code>" for word in words)
        await message.answer(f"已移除 {len(words)} 条拦截词：{body}")

    async def list_words(self, message: Message) -> None:
        actor = await self._group_admin(message)
        if actor is None:
            return
        words = set(self.settings.blocked_words) | await self.storage.blocked_words(actor[2])
        await self._send_chunked_list(message, title="拦截词", items=sorted(words))

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
            admin_state = await self.admins.cached_is_admin(message.chat.id, user_id)
            if admin_state is not False:
                return
            if await self.storage.is_trusted(message.chat.id, user_id):
                return
        elif message.sender_chat is None:
            return

        text = message.text or message.caption or ""
        entities = message.entities if message.text is not None else message.caption_entities
        allowed_domains, blocked_words = await asyncio.gather(
            self.storage.allowed_domains(message.chat.id),
            self.storage.blocked_words(message.chat.id),
        )
        violation = self.detector.detect(
            DetectionInput(
                text=text,
                entity_urls=extract_entity_urls(text, entities),
                is_forwarded=message.forward_origin is not None,
            ),
            allowed_domains=set(self.settings.allowed_domains) | allowed_domains,
            blocked_words=set(self.settings.blocked_words) | blocked_words,
        )
        if violation is None and user_id is not None:
            violation = self.flood.check(message.chat.id, user_id, text)
        if violation is None:
            return
        await self._handle_violation(message, user_id, violation)

    async def _handle_violation(
        self, message: Message, user_id: int | None, _violation: Violation
    ) -> None:
        target_id = user_id or (message.sender_chat.id if message.sender_chat else None)
        if self.settings.dry_run:
            await self.reporter.record(
                chat_id=message.chat.id,
                action="dry_run_hit",
                message_id=message.message_id,
                target_id=target_id,
            )
            return

        try:
            await self.bot.delete_message(message.chat.id, message.message_id)
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
            return

        await self.reporter.record(
            chat_id=message.chat.id,
            action="delete",
            message_id=message.message_id,
            target_id=target_id,
        )
        if user_id is None:
            return

        count = await self.storage.add_warning(message.chat.id, user_id)
        muted = False
        if count >= self.settings.warn_limit:
            try:
                await self._mute_member(
                    message.chat.id, user_id, self.settings.default_mute_seconds
                )
            except TelegramAPIError:
                logger.exception("自动禁言失败 chat_id=%s user_id=%s", message.chat.id, user_id)
            else:
                await self.storage.clear_warnings(message.chat.id, user_id)
                await self.reporter.record(
                    chat_id=message.chat.id,
                    action="auto_mute",
                    target_id=user_id,
                )
                muted = True

        user_label = display_user(message.from_user, user_id)
        suffix = (
            f"，并禁言 {human_duration(self.settings.default_mute_seconds)}"
            if muted
            else f"，警告 {count}/{self.settings.warn_limit}"
        )
        await self._notice(
            message,
            f"已删除 {user_label} 的违规消息{suffix}。",
        )

    async def chat_member_changed(self, event: ChatMemberUpdated) -> None:
        self.admins.invalidate(event.chat.id)
