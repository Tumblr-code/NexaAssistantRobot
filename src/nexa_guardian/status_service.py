from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

from nexa_guardian.config import Settings
from nexa_guardian.storage import AIStats, ChatStats, Storage

DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


class MTProtoProbe(Protocol):
    async def probe(self, chat_id: int) -> object: ...


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
    return f"{label}：删 {stats.deleted}｜警 {stats.warned}｜封 {stats.banned}｜移 {stats.removed}"


def _failure_rate(stats: AIStats) -> float:
    return stats.failed / stats.calls * 100 if stats.calls else 0.0


def _ai_state(*, enabled: bool, reviewer_ready: bool, paused: bool) -> str:
    if not enabled:
        return "配置关闭"
    if not reviewer_ready:
        return "审核器不可用"
    if paused:
        return "已暂停"
    return "运行中"


async def _gather_or_cancel(*tasks: asyncio.Task[Any]) -> tuple[Any, ...]:
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class StatusService:
    """Build status output and own the consolidated AI control surface."""

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        storage: Storage,
        telegram_session: MTProtoProbe,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.storage = storage
        self.telegram_session = telegram_session
        self._mtproto_cache: dict[int, tuple[float, str]] = {}
        self._mtproto_lock = asyncio.Lock()
        self._ai_control_locks: dict[int, asyncio.Lock] = {}

    def ai_control_lock(self, chat_id: int) -> asyncio.Lock:
        return self._ai_control_locks.setdefault(chat_id, asyncio.Lock())

    async def overview(
        self,
        chat_id: int,
        *,
        reviewer_ready: bool,
        ai_failure_streak: int,
    ) -> str:
        now = datetime.now(UTC)
        today_cutoff, week_cutoff = stats_cutoffs(now)
        me = await self.bot.get_me()
        member_task = asyncio.create_task(self.bot.get_chat_member(chat_id, me.id))
        paused_task = asyncio.create_task(self.storage.runtime_setting(chat_id, "ai_paused"))
        today_task = asyncio.create_task(self.storage.chat_stats(chat_id, since=today_cutoff))
        week_task = asyncio.create_task(self.storage.chat_stats(chat_id, since=week_cutoff))
        total_task = asyncio.create_task(self.storage.chat_stats(chat_id))
        warnings_task = asyncio.create_task(self.storage.warning_stats(chat_id))
        ai_week_task = asyncio.create_task(self.storage.ai_stats(chat_id, since=week_cutoff))
        queues_task = asyncio.create_task(
            self.storage.queue_health(now.isoformat(timespec="seconds"))
        )
        mtproto_task = asyncio.create_task(self._mtproto_status(chat_id))
        (
            member,
            paused_value,
            today,
            week,
            total,
            warnings,
            ai_week,
            queues,
            mtproto_status,
        ) = await _gather_or_cancel(
            member_task,
            paused_task,
            today_task,
            week_task,
            total_task,
            warnings_task,
            ai_week_task,
            queues_task,
            mtproto_task,
        )
        is_admin = isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
        can_delete = isinstance(member, ChatMemberOwner) or (
            isinstance(member, ChatMemberAdministrator) and member.can_delete_messages
        )
        can_restrict = isinstance(member, ChatMemberOwner) or (
            isinstance(member, ChatMemberAdministrator) and member.can_restrict_members
        )
        paused = paused_value == "1"
        actual_ai_state = _ai_state(
            enabled=self.settings.ai_enabled,
            reviewer_ready=reviewer_ready,
            paused=paused,
        )
        alerts: list[str] = []
        if ai_failure_streak:
            alerts.append(f"AI 连续失败 {ai_failure_streak} 次")
        if queues.ephemeral_due:
            alerts.append(f"临时消息到期积压 {queues.ephemeral_due}")
        if queues.log_outbox_retrying:
            alerts.append(f"日志重试 {queues.log_outbox_retrying}")
        if queues.log_outbox_due:
            alerts.append(f"日志到期积压 {queues.log_outbox_due}")
        if week.cleanup_failures:
            alerts.append(f"近 7 天成员解封失败 {week.cleanup_failures}")
        alert_line = "；".join(alerts) if alerts else "无"
        purge_line = (
            f"\n历史清理：今日 {today.history_purges}｜"
            f"近 7 天 {week.history_purges}｜累计 {total.history_purges}"
        )
        return (
            "<b>Nexa Guardian 状态</b>\n"
            f"机器人：@{html.escape(me.username or str(me.id))}\n"
            f"目标群：<code>{chat_id}</code>｜白名单："
            f"{'是' if chat_id in self.settings.allowed_chat_ids else '否'}\n"
            f"模式：{'观察' if self.settings.dry_run else '执行'}｜AI：{actual_ai_state}\n"
            f"权限：管理 {'有' if is_admin else '缺'}｜删除 {'有' if can_delete else '缺'}｜"
            f"限制 {'有' if can_restrict else '缺'}\n"
            f"MTProto：{html.escape(mtproto_status)}\n\n"
            "<b>管理统计（北京时间）</b>\n"
            f"{stats_line('今日', today)}\n"
            f"{stats_line('近 7 天', week)}\n"
            f"{stats_line('累计', total)}"
            f"{purge_line}\n"
            f"未清警告：{warnings.users} 人｜{warnings.total} 次\n\n"
            "<b>AI 近 7 天</b>\n"
            f"调用 {ai_week.calls}｜广告 {ai_week.ads}｜失败率 "
            f"{_failure_rate(ai_week):.1f}%｜P95 {ai_week.gateway_latency_p95_ms} ms\n"
            f"队列：临时 {queues.ephemeral_total}（到期 {queues.ephemeral_due}）｜"
            f"日志 {queues.log_outbox_total}（到期 {queues.log_outbox_due}｜"
            f"重试 {queues.log_outbox_retrying}）\n"
            f"运行告警：{html.escape(alert_line)}"
        )

    async def ai_detail(
        self,
        chat_id: int,
        *,
        reviewer_ready: bool,
        ai_failure_streak: int,
    ) -> str:
        today_cutoff, week_cutoff = stats_cutoffs()
        paused_task = asyncio.create_task(self.storage.runtime_setting(chat_id, "ai_paused"))
        today_task = asyncio.create_task(self.storage.ai_stats(chat_id, since=today_cutoff))
        week_task = asyncio.create_task(self.storage.ai_stats(chat_id, since=week_cutoff))
        total_task = asyncio.create_task(self.storage.ai_stats(chat_id))
        signatures_task = asyncio.create_task(
            self.storage.ai_signatures(chat_id, statuses=("active",))
        )
        paused_value, today, week, total, signatures = await _gather_or_cancel(
            paused_task,
            today_task,
            week_task,
            total_task,
            signatures_task,
        )
        paused = paused_value == "1"
        actual_ai_state = _ai_state(
            enabled=self.settings.ai_enabled,
            reviewer_ready=reviewer_ready,
            paused=paused,
        )
        return (
            "<b>AI 审核状态</b>\n"
            f"实际状态：{actual_ai_state}\n"
            f"模型：<code>{html.escape(self.settings.ai_model)}</code>｜思考："
            f"<code>{html.escape(self.settings.ai_reasoning_effort)}</code>\n"
            f"并发：{self.settings.ai_concurrency}｜超时：{self.settings.ai_timeout_seconds:g}s\n"
            f"连续失败：{ai_failure_streak}\n"
            f"Active 特征：{len(signatures)}\n\n"
            f"今日：调用 {today.calls}｜广告 {today.ads}｜失败率 {_failure_rate(today):.1f}%\n"
            f"近 7 天：调用 {week.calls}｜广告 {week.ads}｜失败率 "
            f"{_failure_rate(week):.1f}%｜P95 {week.gateway_latency_p95_ms} ms\n"
            f"累计：调用 {total.calls}｜广告 {total.ads}｜失败 {total.failed}"
        )

    async def set_ai_paused(self, chat_id: int, paused: bool) -> bool:
        expected = "1" if paused else "0"
        current = await self.storage.runtime_setting(chat_id, "ai_paused")
        if current == expected:
            return False
        await self.storage.set_runtime_setting(chat_id, "ai_paused", expected)
        return True

    async def ai_word_pages(self, chat_id: int, *, max_length: int = 3500) -> tuple[str, ...]:
        signatures = await self.storage.ai_signatures(chat_id, statuses=("active",))
        items = [
            f"<code>{signature.id}</code> · <code>{html.escape(signature.display_signature)}</code>"
            for signature in signatures
        ]
        if not items:
            return ("<b>AI Active 特征（0）</b>\n（空）",)
        pages: list[str] = []
        body = ""
        for item in items:
            addition = item if not body else f"　{item}"
            if body and len(body) + len(addition) > max_length:
                pages.append(body)
                body = item
            else:
                body += addition
        if body:
            pages.append(body)
        total = len(items)
        return tuple(
            f"<b>AI Active 特征（{total}）"
            f"{' ' + str(index) + '/' + str(len(pages)) if len(pages) > 1 else ''}</b>\n{page}"
            for index, page in enumerate(pages, start=1)
        )

    async def disable_ai_signature(self, chat_id: int, signature_id: int) -> bool:
        return await self.storage.disable_ai_signature(chat_id, str(signature_id))

    async def _mtproto_status(self, chat_id: int) -> str:
        if not self.settings.mtproto_ready:
            return "未配置凭据"
        now = time.monotonic()
        cached = self._mtproto_cache.get(chat_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        async with self._mtproto_lock:
            now = time.monotonic()
            cached = self._mtproto_cache.get(chat_id)
            if cached is not None and cached[0] > now:
                return cached[1]
            try:
                result = await self.telegram_session.probe(chat_id)
            except asyncio.TimeoutError:
                detail = "验证超时"
            except Exception as exc:
                detail = f"验证失败（{type(exc).__name__}）"
            else:
                raw_detail = getattr(result, "detail", None)
                if isinstance(raw_detail, str) and raw_detail:
                    detail = raw_detail
                elif getattr(result, "verified", False):
                    detail = "会话身份已验证"
                else:
                    detail = "仅配置凭据，身份未验证"
            if detail != "任务运行中":
                self._mtproto_cache[chat_id] = (now + 60, detail)
            return detail
