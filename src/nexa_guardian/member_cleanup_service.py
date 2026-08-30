from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import Message, TelegramObject

from nexa_guardian.config import Settings
from nexa_guardian.member_activity import (
    MemberActivityPlan,
    MemberActivityPlanBusy,
    MemberActivityPlanNotFound,
    MemberActivityTracker,
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
from nexa_guardian.reporting import ActionReporter
from nexa_guardian.storage import Storage

logger = logging.getLogger(__name__)
UTC = timezone.utc
MEMBER_CLEANUP_DAYS = 30
MEMBER_CLEANUP_LIMIT = 50
CONFIRM_TTL_SECONDS = 10 * 60
KICK_FALLBACK_SECONDS = 5 * 60
RETRY_AFTER_MAX_SECONDS = 15
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}

FinishProcessing = Callable[[Message, Message | None, str], Awaitable[None]]
MemberLock = Callable[[int, int], asyncio.Lock]
ActiveRaffleParticipants = Callable[[int], Awaitable[frozenset[int]]]


class MemberActivityMiddleware(BaseMiddleware):
    """Observe activity while a member-cleanup preview is active."""

    def __init__(
        self,
        tracker: MemberActivityTracker,
        *,
        allowed_chat_ids: frozenset[int],
    ) -> None:
        self.tracker = tracker
        self.allowed_chat_ids = allowed_chat_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
        user = event.from_user
        if (
            event.chat.type in GROUP_TYPES
            and event.chat.id in self.allowed_chat_ids
            and user is not None
            and not user.is_bot
        ):
            observed = event.edit_date or event.date
            if not isinstance(observed, datetime):
                observed = datetime.fromtimestamp(observed, UTC)
            self.tracker.note(
                event.chat.id,
                user.id,
                observed_at=observed.astimezone(UTC),
            )
        return await handler(event, data)


@dataclass(frozen=True, slots=True)
class PendingMemberCleanup:
    actor_id: int
    chat_id: int
    cutoff: datetime
    created_at: datetime
    members: tuple[MemberCandidate, ...]
    activity_plan: MemberActivityPlan


async def telegram_call_with_bounded_retry(
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Retry one short Bot API flood wait without stalling the cleanup."""

    try:
        return await operation()
    except TelegramRetryAfter as exc:
        if exc.retry_after > RETRY_AFTER_MAX_SECONDS:
            raise
        await asyncio.sleep(max(0, exc.retry_after) + 0.1)
        return await operation()


class MemberCleanupService:
    """Own preview, confirmation, activity protection and reversible removal."""

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        storage: Storage,
        scanner: MemberScanner,
        reporter: ActionReporter,
        *,
        finish_processing: FinishProcessing,
        member_lock: MemberLock,
        active_raffle_participants: ActiveRaffleParticipants | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.storage = storage
        self.scanner = scanner
        self.reporter = reporter
        self.finish_processing = finish_processing
        self.member_lock = member_lock
        self.active_raffle_participants = (
            active_raffle_participants or self._load_active_raffle_participants
        )
        self.activity = MemberActivityTracker(default_ttl_seconds=CONFIRM_TTL_SECONDS)
        self._plans: dict[int, PendingMemberCleanup] = {}

    async def command(
        self,
        message: Message,
        arguments: tuple[str, ...],
        *,
        actor_id: int,
        chat_id: int,
    ) -> None:
        self._prune_plans()
        if len(arguments) == 2 and arguments[1].casefold() == "confirm":
            await self._confirm(
                message,
                actor_id=actor_id,
                confirmation_code=arguments[0].upper(),
            )
            return
        if arguments:
            await message.answer(
                "用法：<code>/del members</code>\n确认：<code>/del members 确认码 confirm</code>"
            )
            return
        await self._preview(message, actor_id=actor_id, chat_id=chat_id)

    def _prune_plans(self) -> None:
        self.activity.expire()
        for session_id, plan in tuple(self._plans.items()):
            try:
                self.activity.active_ids(plan.activity_plan)
            except MemberActivityPlanNotFound:
                self._plans.pop(session_id, None)

    async def _preview(self, message: Message, *, actor_id: int, chat_id: int) -> None:
        preview_started_at = datetime.now(UTC).replace(microsecond=0)
        cutoff = preview_started_at - timedelta(days=MEMBER_CLEANUP_DAYS)
        try:
            activity_session = self.activity.begin(
                actor_id=actor_id,
                chat_id=chat_id,
                cutoff=cutoff,
                created_at=preview_started_at,
            )
        except MemberActivityPlanBusy:
            await message.answer("已有成员清理任务正在执行，请等待完成后再试。")
            return
        progress = await message.answer("正在扫描成员并核验最近 30 天群活动…")
        try:
            result = await asyncio.wait_for(
                self.scanner.scan(chat_id, cutoff=cutoff),
                timeout=120,
            )
        except asyncio.TimeoutError:
            self.activity.finish(activity_session)
            await self.finish_processing(
                message,
                progress,
                "成员扫描超过 120 秒，未生成计划。",
            )
            return
        except MemberScanError as exc:
            self.activity.finish(activity_session)
            await self.finish_processing(
                message,
                progress,
                f"成员扫描失败：{html.escape(str(exc))}",
            )
            return

        trusted_task = asyncio.create_task(self.storage.trusted_user_ids(chat_id))
        raffle_task = asyncio.ensure_future(self.active_raffle_participants(chat_id))
        try:
            trusted_ids = await trusted_task
            protected_raffle_ids = await raffle_task
        except Exception:
            for task in (trusted_task, raffle_task):
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self.activity.finish(activity_session)
            logger.exception("成员清理保护名单读取失败 chat_id=%s", chat_id)
            await self.finish_processing(
                message,
                progress,
                "成员保护名单读取失败，未生成清理计划。",
            )
            return
        active_during_scan = self.activity.active_ids(activity_session)
        eligible = [
            candidate
            for candidate in result.candidates
            if candidate.user_id not in trusted_ids
            and candidate.user_id not in protected_raffle_ids
            and candidate.user_id not in active_during_scan
        ]
        if not eligible:
            self.activity.finish(activity_session)
            await self.finish_processing(
                message,
                progress,
                f"成员扫描完成：共核验 {result.participants_scanned} 人，没有清理候选。"
                + (
                    "\n群历史读取不完整，本次未使用“30 天无发言”条件。"
                    if not result.history_complete
                    else ""
                ),
            )
            return

        selected = tuple(eligible[:MEMBER_CLEANUP_LIMIT])
        self._plans.clear()
        code = secrets.token_hex(3).upper()
        activity_plan = self.activity.bind(
            activity_session,
            confirmation_code=code,
            candidate_ids=(candidate.user_id for candidate in selected),
        )
        self._plans[activity_plan.session.session_id] = PendingMemberCleanup(
            actor_id=actor_id,
            chat_id=chat_id,
            cutoff=cutoff,
            created_at=preview_started_at,
            members=selected,
            activity_plan=activity_plan,
        )
        lines = [
            f"<code>{candidate.user_id}</code>｜{self.reason_text(candidate)}"
            for candidate in selected
        ]
        await self.finish_processing(
            message,
            progress,
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
                if len(eligible) > MEMBER_CLEANUP_LIMIT
                else ""
            )
            + f"\n10 分钟内发送：<code>/del members {code} confirm</code>",
        )

    async def _confirm(
        self,
        message: Message,
        *,
        actor_id: int,
        confirmation_code: str,
    ) -> None:
        activity_plan = self.activity.start_execution(
            actor_id=actor_id,
            confirmation_code=confirmation_code,
        )
        if activity_plan is None:
            await message.answer("确认码无效或已过期；请重新生成清理计划。")
            return
        session_id = activity_plan.session.session_id
        plan = self._plans.get(session_id)
        if plan is None:
            self.activity.finish(activity_plan)
            await message.answer("清理计划已失效；请重新生成预览。")
            return
        executing_plan = PendingMemberCleanup(
            actor_id=plan.actor_id,
            chat_id=plan.chat_id,
            cutoff=plan.cutoff,
            created_at=plan.created_at,
            members=plan.members,
            activity_plan=activity_plan,
        )
        try:
            await self._execute(message, executing_plan)
        finally:
            self.activity.finish(activity_plan)
            self._plans.pop(session_id, None)

    async def _execute(self, message: Message, plan: PendingMemberCleanup) -> None:
        progress = await message.answer("正在复核候选成员及预览后的群活动…")
        try:
            current_states, active_since_preview = await asyncio.wait_for(
                self.scanner.current_members(
                    plan.chat_id,
                    cutoff=plan.cutoff,
                    activity_since=plan.created_at,
                    user_ids=frozenset(candidate.user_id for candidate in plan.members),
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            await self.finish_processing(
                message,
                progress,
                "成员复核超过 120 秒，未执行清理。",
            )
            return
        except MemberScanError as exc:
            await self.finish_processing(
                message,
                progress,
                f"成员复核失败，未执行清理：{html.escape(str(exc))}",
            )
            return

        trusted_task = asyncio.create_task(self.storage.trusted_user_ids(plan.chat_id))
        raffle_task = asyncio.ensure_future(self.active_raffle_participants(plan.chat_id))
        try:
            trusted_ids = await trusted_task
            protected_raffle_ids = await raffle_task
        except Exception:
            for task in (trusted_task, raffle_task):
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            logger.exception("成员清理复核保护名单读取失败 chat_id=%s", plan.chat_id)
            await self.finish_processing(
                message,
                progress,
                "成员保护名单复核失败，未执行清理。",
            )
            return
        active_since_preview = frozenset(active_since_preview).union(
            self.activity.active_ids(plan.activity_plan)
        )
        cleaned = skipped = failed = unban_failed = not_processed = 0
        failed_target_ids: list[int] = []
        admin_statuses = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
        absent_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}

        for index, candidate in enumerate(plan.members):
            user_id = candidate.user_id
            async with self.member_lock(plan.chat_id, user_id):
                try:
                    member = await telegram_call_with_bounded_retry(
                        lambda target_id=user_id: self.bot.get_chat_member(
                            plan.chat_id,
                            target_id,
                        )
                    )
                except TelegramRetryAfter:
                    logger.warning(
                        "清理前核验成员触发长时间限流 chat_id=%s user_id=%s",
                        plan.chat_id,
                        user_id,
                    )
                    failed += 1
                    failed_target_ids.append(user_id)
                    not_processed = len(plan.members) - index - 1
                    break
                except TelegramAPIError:
                    logger.exception(
                        "清理前核验成员失败 chat_id=%s user_id=%s",
                        plan.chat_id,
                        user_id,
                    )
                    failed += 1
                    failed_target_ids.append(user_id)
                    continue

                state = current_states.get(user_id)
                recently_active = user_id in active_since_preview or user_id in (
                    self.activity.active_ids(plan.activity_plan)
                )
                if (
                    state is None
                    or state.administrator
                    or state.bot
                    or user_id in self.settings.authorized_admin_ids
                    or user_id in trusted_ids
                    or user_id in protected_raffle_ids
                    or member.status in admin_statuses | absent_statuses
                    or getattr(getattr(member, "user", None), "is_bot", False)
                    or not self.candidate_still_matches(
                        candidate,
                        current_status_reason=state.status_reason,
                        current_deleted=state.deleted,
                        current_joined_at=state.joined_at,
                        cutoff=plan.cutoff,
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
                            until_date=datetime.now(UTC) + timedelta(seconds=KICK_FALLBACK_SECONDS),
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
                    failed_target_ids.append(user_id)
                    not_processed = len(plan.members) - index - 1
                    break
                except TelegramAPIError:
                    logger.exception(
                        "长期未发言成员移出失败 chat_id=%s user_id=%s",
                        plan.chat_id,
                        user_id,
                    )
                    failed += 1
                    failed_target_ids.append(user_id)
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
                        action="member_cleanup_unban_failed",
                        actor_id=plan.actor_id,
                        target_id=user_id,
                    )
                    failed += 1
                    failed_target_ids.append(user_id)
                    unban_failed += 1
                    not_processed = len(plan.members) - index - 1
                    break
                await self.reporter.record(
                    chat_id=plan.chat_id,
                    action="member_cleanup",
                    actor_id=plan.actor_id,
                    target_id=user_id,
                )
                cleaned += 1

        failure_ids = (
            "失败目标："
            + "、".join(f"<code>{user_id}</code>" for user_id in failed_target_ids)
            + "。\n"
            if failed_target_ids
            else ""
        )
        await self.finish_processing(
            message,
            progress,
            f"清理完成：已移出 {cleaned} 人，跳过 {skipped} 人，失败 {failed} 人，"
            f"未继续处理 {not_processed} 人。\n"
            + failure_ids
            + (
                f"其中 {unban_failed} 人解除封禁失败；临时封禁最多 5 分钟后自动失效，"
                "请按上方用户 ID 核查。\n"
                if unban_failed
                else ""
            )
            + "本次候选依据为 Telegram 账号状态或最近 30 天群内发言。",
        )

    async def _load_active_raffle_participants(self, chat_id: int) -> frozenset[int]:
        raffle = await self.storage.active_raffle(chat_id)
        if raffle is None:
            return frozenset()
        return frozenset(await self.storage.raffle_entry_ids(raffle.id))

    @staticmethod
    def reason_text(candidate: MemberCandidate) -> str:
        labels = {
            REASON_DELETED: "已注销",
            REASON_OFFLINE: "精确离线超过 30 天",
            REASON_LAST_MONTH: "Telegram 模糊状态：约一周至一个月",
            REASON_SILENT: "30 天无发言",
        }
        return "、".join(labels[reason] for reason in candidate.reasons)

    @staticmethod
    def candidate_still_matches(
        candidate: MemberCandidate,
        *,
        current_status_reason: str | None,
        current_deleted: bool,
        current_joined_at: datetime | None,
        cutoff: datetime,
        recently_active: bool,
    ) -> bool:
        if recently_active:
            return False
        status_reasons = {REASON_OFFLINE, REASON_LAST_MONTH}
        deleted_match = REASON_DELETED in candidate.reasons and current_deleted
        status_match = bool(status_reasons.intersection(candidate.reasons)) and (
            current_status_reason in status_reasons
        )
        silent_match = (
            REASON_SILENT in candidate.reasons
            and current_joined_at is not None
            and current_joined_at <= cutoff
        )
        return deleted_match or status_match or silent_match
