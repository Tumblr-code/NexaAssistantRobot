from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Literal

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message, User

from nexa_guardian.admin import AdminService
from nexa_guardian.commands import command_payload
from nexa_guardian.config import Settings
from nexa_guardian.detection import parse_duration
from nexa_guardian.ephemeral import suspend_ephemeral_tracking
from nexa_guardian.raffle_runtime import RaffleRuntime, verify_raffle_members
from nexa_guardian.status_service import action_time
from nexa_guardian.storage import PendingRaffleResult, Raffle, Storage

logger = logging.getLogger(__name__)
UTC = timezone.utc
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
RAFFLE_MIN_SECONDS = 60
RAFFLE_MAX_SECONDS = 30 * 24 * 60 * 60
RAFFLE_WORKER_INTERVAL_SECONDS = 2.0
RAFFLE_CARD_DEBOUNCE_SECONDS = 2.0
RAFFLE_CARD_RECONCILE_SECONDS = 60.0
RAFFLE_RESULT_RETRY_SECONDS = 30
RAFFLE_RESULT_ANCHOR_TEXT = "🎲 正在生成抽奖结果…"


class _RaffleResultAnchorMissing(RuntimeError):
    pass


class RaffleService:
    """Own the complete keyword-raffle lifecycle and its in-memory hot path."""

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        storage: Storage,
        admins: AdminService,
        *,
        runtime: RaffleRuntime | None = None,
        worker_interval_seconds: float = RAFFLE_WORKER_INTERVAL_SECONDS,
        card_debounce_seconds: float = RAFFLE_CARD_DEBOUNCE_SECONDS,
        card_reconcile_seconds: float = RAFFLE_CARD_RECONCILE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if worker_interval_seconds <= 0 or card_debounce_seconds < 0 or card_reconcile_seconds <= 0:
            raise ValueError(
                "raffle worker and reconciliation intervals must be positive "
                "and debounce non-negative"
            )
        self.bot = bot
        self.settings = settings
        self.storage = storage
        self.admins = admins
        self.runtime = runtime or RaffleRuntime(storage)
        self.worker_interval_seconds = worker_interval_seconds
        self.card_debounce_seconds = card_debounce_seconds
        self.card_reconcile_seconds = card_reconcile_seconds
        self._monotonic = monotonic
        self._next_card_reconcile_at = self._monotonic() + card_reconcile_seconds
        self._card_updates: dict[int, tuple[Raffle, int]] = {}
        self._card_tasks: dict[int, asyncio.Task[None]] = {}
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = False

    def chat_lock(self, chat_id: int) -> asyncio.Lock:
        return self.runtime.chat_lock(chat_id)

    async def ensure_active(self, chat_id: int) -> Raffle | None:
        """Return cached state or explicitly recover it from Storage.

        Callers that need an atomic follow-up operation should hold chat_lock()
        around this method and that operation.
        """

        raffle = self.runtime.get(chat_id)
        if raffle is not None:
            return raffle
        raffle = await self.storage.active_raffle(chat_id)
        if raffle is None:
            return None
        self.runtime.set(raffle)
        return raffle

    async def active_participant_ids(self, chat_id: int) -> frozenset[int]:
        """Return entrants protected from member cleanup for the active raffle."""

        async with self.chat_lock(chat_id):
            raffle = await self.ensure_active(chat_id)
            if raffle is None:
                return frozenset()
            return frozenset(await self.storage.raffle_entry_ids(raffle.id))

    async def startup(self) -> None:
        self._stopping = False
        await self._clean_pending()
        await self._recover_pending_results(force=True)
        await self._sync_active_cards()
        self._next_card_reconcile_at = self._monotonic() + self.card_reconcile_seconds
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._run_worker(),
                name="nexa-raffle-worker",
            )

    async def shutdown(self) -> None:
        self._stopping = True
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.flush_card_updates()

    async def keyword_filter(self, message: Message) -> bool | dict[str, Raffle]:
        anonymous_admin = (
            message.sender_chat is not None and message.sender_chat.id == message.chat.id
        )
        if (
            message.chat.type not in GROUP_TYPES
            or message.chat.id not in self.settings.allowed_chat_ids
            or message.text is None
            or message.is_automatic_forward
        ):
            return False
        if not anonymous_admin and (
            message.from_user is None or message.from_user.is_bot or message.sender_chat is not None
        ):
            return False
        raffle = self.runtime.keyword_match(message.chat.id, message.text)
        return {"raffle": raffle} if raffle is not None else False

    async def pin_service_filter(self, message: Message) -> bool:
        pinned = message.pinned_message
        if (
            pinned is None
            or message.chat.type not in GROUP_TYPES
            or message.chat.id not in self.settings.allowed_chat_ids
        ):
            return False
        cached = self.runtime.get(message.chat.id)
        if cached is not None and cached.message_id == pinned.message_id:
            return True
        return await self.storage.raffle_for_message(message.chat.id, pinned.message_id) is not None

    async def delete_pin_service_message(self, message: Message) -> None:
        try:
            await self.bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            logger.debug("删除抽奖置顶服务消息失败 chat_id=%s", message.chat.id)

    async def join(self, message: Message, raffle: Raffle) -> None:
        anonymous_admin = (
            message.sender_chat is not None and message.sender_chat.id == message.chat.id
        )
        if anonymous_admin:
            try:
                actor = await self.admins.unique_anonymous_actor(
                    message.chat.id,
                    self.settings.authorized_admin_ids,
                )
            except TelegramAPIError:
                actor = None
                logger.exception("核验匿名抽奖参与者失败 chat_id=%s", message.chat.id)
            if actor is None:
                await self._delete_keyword_message(message)
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
        await self._delete_keyword_message(message)
        if count == 0:
            await self.bot.send_message(message.chat.id, "该抽奖已结束。")
            return
        if joined and raffle.message_id is not None:
            self.schedule_card_update(raffle, count)
        mention = self.participant_mention(participant_user, participant_id)
        text = (
            f"{mention}，已成功参与抽奖！🎉\n当前共有 {count} 人参与。"
            if joined
            else f"{mention}，你已经参与过啦，本次不会重复登记。"
        )
        await self.bot.send_message(message.chat.id, text)

    async def command(
        self,
        message: Message,
        *,
        actor_id: int,
        chat_id: int,
    ) -> None:
        payload = command_payload(message)
        if not payload:
            await message.answer(
                "用法：<code>/lottery 30m 1 参与 奖品</code>\n"
                "管理：<code>/lottery end|cancel|reroll</code>"
            )
            return
        operation = payload.casefold()
        if operation == "end":
            raffle = await self.ensure_active(chat_id)
            if raffle is None:
                await message.answer("当前没有进行中的抽奖。")
                return
            await self.finalize(raffle, notify_admin=True)
            return
        if operation == "cancel":
            await self.cancel_active(chat_id)
            return
        if operation == "reroll":
            await self._reroll_latest(chat_id)
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

        async with self.chat_lock(chat_id):
            if await self.ensure_active(chat_id) is not None:
                await message.answer("当前已有进行中的抽奖，请先结束或取消。")
                return
            try:
                raffle = await self.storage.create_raffle(
                    chat_id=chat_id,
                    creator_id=actor_id,
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
            active = await self._publish_new_raffle(message, raffle)
            if active is None:
                return
        await message.answer(
            "🎉 <b>抽奖已发布并置顶</b>\n"
            f"发送关键词 <code>{html.escape(active.keyword or '')}</code> 即可参与。"
        )

    async def _publish_new_raffle(self, message: Message, raffle: Raffle) -> Raffle | None:
        card: Message | None = None
        pinned = False
        try:
            card = await self.bot.send_message(
                raffle.chat_id,
                self.card_text(raffle, 0),
            )
            if not await self.storage.set_raffle_message(raffle.id, card.message_id):
                raise RuntimeError("抽奖卡片绑定失败")
            pinned = bool(
                await self.bot.pin_chat_message(
                    raffle.chat_id,
                    card.message_id,
                    disable_notification=True,
                )
            )
            if not pinned:
                raise RuntimeError("Telegram 未确认置顶")
            await self._ensure_admin_result_placeholder(raffle)
            if not await self.storage.activate_raffle(raffle.id):
                raise RuntimeError("抽奖激活失败")
            active = replace(
                raffle,
                message_id=card.message_id,
                publication_state="active",
            )
            if not self.runtime.set(active):
                raise RuntimeError("抽奖缓存激活失败")
            await self._keep_card_persistent(raffle.chat_id, card.message_id)
            return active
        except Exception:
            self.runtime.drop(raffle.chat_id, raffle_id=raffle.id)
            if card is not None:
                if pinned:
                    with contextlib.suppress(TelegramAPIError):
                        await self.bot.unpin_chat_message(
                            raffle.chat_id,
                            message_id=card.message_id,
                        )
                await self._delete_card_or_expire(raffle.chat_id, card.message_id)
            await self.storage.discard_pending_raffle(raffle.id)
            await self._cancel_admin_result_placeholder(
                raffle,
                text=f"抽奖 #{raffle.id} 发布未完成，已取消。",
                remove_setting=True,
            )
            logger.exception(
                "发布抽奖卡片失败 chat_id=%s raffle_id=%s",
                raffle.chat_id,
                raffle.id,
            )
            await message.answer("抽奖发布失败，已安全取消。")
            return None

    @staticmethod
    def card_text(raffle: Raffle, count: int) -> str:
        return (
            "<b>🎁 Nexa 群抽奖</b>\n\n"
            f"奖品：{html.escape(raffle.prize)}\n"
            f"参与关键词：<code>{html.escape(raffle.keyword or '')}</code>\n"
            f"中奖人数：{raffle.winner_count}\n"
            f"参与人数：{count}\n"
            f"开奖时间：{action_time(raffle.ends_at)}（北京时间）"
        )

    def schedule_card_update(self, raffle: Raffle, count: int) -> None:
        previous = self._card_updates.get(raffle.id)
        if previous is None or count >= previous[1]:
            self._card_updates[raffle.id] = (raffle, count)
        task = self._card_tasks.get(raffle.id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._debounced_card_update(raffle.id),
                name=f"nexa-raffle-card-{raffle.id}",
            )
            self._card_tasks[raffle.id] = task
            task.add_done_callback(
                lambda completed, raffle_id=raffle.id: self._card_update_done(
                    raffle_id,
                    completed,
                )
            )

    async def _debounced_card_update(self, raffle_id: int) -> None:
        await asyncio.sleep(self.card_debounce_seconds)
        update = self._card_updates.pop(raffle_id, None)
        if update is not None:
            await self._edit_card(*update)

    def _card_update_done(self, raffle_id: int, task: asyncio.Task[None]) -> None:
        if self._card_tasks.get(raffle_id) is task:
            self._card_tasks.pop(raffle_id, None)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "抽奖人数更新任务失败 raffle_id=%s",
                raffle_id,
                exc_info=(type(exception), exception, exception.__traceback__),
            )
        pending = self._card_updates.get(raffle_id)
        if pending is not None and not self._stopping:
            self.schedule_card_update(*pending)

    async def _edit_card(self, raffle: Raffle, count: int) -> None:
        if raffle.message_id is None:
            return
        try:
            await self.bot.edit_message_text(
                self.card_text(raffle, count),
                chat_id=raffle.chat_id,
                message_id=raffle.message_id,
            )
            await self.storage.ack_ephemeral(raffle.chat_id, raffle.message_id)
        except TelegramBadRequest as exc:
            detail = str(exc).casefold()
            if "message is not modified" in detail:
                await self.storage.ack_ephemeral(raffle.chat_id, raffle.message_id)
                return
            if "message not found" in detail or "message to edit not found" in detail:
                await self._recreate_card(raffle, count)
                return
            logger.debug("更新抽奖人数失败 raffle_id=%s", raffle.id)
        except TelegramAPIError:
            logger.debug("更新抽奖人数失败 raffle_id=%s", raffle.id)

    async def _recreate_card(self, raffle: Raffle, count: int) -> None:
        async with self.chat_lock(raffle.chat_id):
            current = await self.storage.raffle(raffle.id)
            if (
                current is None
                or current.status != "running"
                or current.publication_state != "active"
                or current.message_id is None
            ):
                self.runtime.drop(raffle.chat_id, raffle_id=raffle.id)
                return
            if current.message_id != raffle.message_id:
                self.runtime.set(current)
                self.schedule_card_update(current, count)
                return
            replacement: Message | None = None
            pinned = False
            try:
                replacement = await self.bot.send_message(
                    current.chat_id,
                    self.card_text(current, count),
                )
                pinned = bool(
                    await self.bot.pin_chat_message(
                        current.chat_id,
                        replacement.message_id,
                        disable_notification=True,
                    )
                )
                if not pinned:
                    raise RuntimeError("Telegram 未确认重新置顶")
                replaced = await self.storage.replace_active_raffle_message(
                    current.id,
                    current.message_id,
                    replacement.message_id,
                )
                if not replaced:
                    raise RuntimeError("抽奖卡片已由其他任务恢复")
            except (TelegramAPIError, RuntimeError):
                if replacement is not None:
                    if pinned:
                        with contextlib.suppress(TelegramAPIError):
                            await self.bot.unpin_chat_message(
                                current.chat_id,
                                message_id=replacement.message_id,
                            )
                    await self._delete_card_or_expire(
                        current.chat_id,
                        replacement.message_id,
                    )
                latest = await self.storage.raffle(current.id)
                if latest is not None:
                    self.runtime.set(latest)
                logger.warning(
                    "恢复抽奖卡片失败 chat_id=%s raffle_id=%s",
                    current.chat_id,
                    current.id,
                )
                return
            updated = replace(current, message_id=replacement.message_id)
            self.runtime.set(updated)
            await self._keep_card_persistent(current.chat_id, replacement.message_id)
            logger.info(
                "抽奖卡片已恢复 chat_id=%s raffle_id=%s message_id=%s",
                current.chat_id,
                current.id,
                replacement.message_id,
            )

    async def _sync_active_cards(self) -> None:
        for chat_id in self.settings.allowed_chat_ids:
            async with self.chat_lock(chat_id):
                raffle = await self.storage.active_raffle(chat_id)
                if raffle is None:
                    self.runtime.drop(chat_id)
                    continue
                if not self.runtime.set(raffle):
                    continue
                self.schedule_card_update(
                    raffle,
                    await self.storage.raffle_entry_count(raffle.id),
                )

    async def _reconcile_active_cards_if_due(self) -> None:
        now = self._monotonic()
        if now < self._next_card_reconcile_at:
            return
        self._next_card_reconcile_at = now + self.card_reconcile_seconds
        await self._sync_active_cards()

    async def flush_card_updates(self) -> None:
        tasks = tuple(self._card_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._card_tasks.clear()
        updates = tuple(self._card_updates.values())
        self._card_updates.clear()
        for raffle, count in updates:
            await self._edit_card(raffle, count)

    async def _cancel_card_update(self, raffle_id: int) -> None:
        self._card_updates.pop(raffle_id, None)
        task = self._card_tasks.pop(raffle_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @staticmethod
    def participant_mention(user: User | None, user_id: int) -> str:
        if user is not None and user.username:
            return f"@{html.escape(user.username)}"
        label = html.escape(user.full_name) if user is not None else "用户"
        return f'<a href="tg://user?id={user_id}">{label}</a>'

    async def _delete_keyword_message(self, message: Message) -> None:
        try:
            await self.bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            logger.debug(
                "删除抽奖关键词消息失败 chat_id=%s message_id=%s",
                message.chat.id,
                message.message_id,
            )

    async def _unpin(self, raffle: Raffle) -> bool:
        if raffle.message_id is None:
            return True
        try:
            return bool(
                await self.bot.unpin_chat_message(
                    raffle.chat_id,
                    message_id=raffle.message_id,
                )
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

    async def _clean_pending(self) -> None:
        for raffle in await self.storage.pending_raffles(chat_ids=self.settings.allowed_chat_ids):
            if raffle.message_id is not None:
                await self._unpin(raffle)
                await self._delete_card_or_expire(raffle.chat_id, raffle.message_id)
            await self.storage.discard_pending_raffle(raffle.id)
            await self._cancel_admin_result_placeholder(
                raffle,
                text=f"抽奖 #{raffle.id} 发布未完成，已取消。",
                remove_setting=True,
            )

    async def _delete_card_or_expire(self, chat_id: int, message_id: int) -> None:
        try:
            deleted = await self.bot.delete_message(chat_id, message_id)
            if deleted is False:
                raise RuntimeError("Telegram 未确认删除抽奖卡片")
        except (TelegramAPIError, RuntimeError):
            delete_at = (
                datetime.now(UTC) + timedelta(seconds=self.settings.notice_ttl_seconds)
            ).isoformat(timespec="microseconds")
            try:
                await self.storage.enqueue_ephemeral(chat_id, message_id, delete_at)
            except Exception:
                logger.exception(
                    "抽奖孤立卡片清理入队失败 chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
        else:
            try:
                await self.storage.ack_ephemeral(chat_id, message_id)
            except Exception:
                logger.exception(
                    "已删除抽奖卡片的临时队列记录清理失败 chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )

    async def _keep_card_persistent(self, chat_id: int, message_id: int) -> None:
        try:
            await self.storage.ack_ephemeral(chat_id, message_id)
        except Exception:
            logger.exception(
                "抽奖卡片临时队列豁免失败 chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

    async def _schedule_card_cleanup(self, raffle: Raffle) -> None:
        if raffle.message_id is None:
            return
        await self._schedule_message_cleanup(raffle.chat_id, raffle.message_id)

    async def _schedule_message_cleanup(self, chat_id: int, message_id: int) -> None:
        delete_at = self._message_cleanup_deadline()
        try:
            await self.storage.enqueue_ephemeral(chat_id, message_id, delete_at)
        except Exception:
            logger.exception(
                "抽奖结果清理入队失败 chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

    def _message_cleanup_deadline(self) -> str:
        return (datetime.now(UTC) + timedelta(seconds=self.settings.notice_ttl_seconds)).isoformat(
            timespec="microseconds"
        )

    async def _valid_entries(self, raffle: Raffle) -> tuple[int, ...]:
        user_ids = await self.storage.raffle_entry_ids(raffle.id)

        async def valid(user_id: int) -> bool:
            try:
                member = await self.bot.get_chat_member(raffle.chat_id, user_id)
            except TelegramAPIError:
                return False
            user = getattr(member, "user", None)
            if getattr(user, "is_bot", False):
                return False
            return not (
                member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
                or (
                    member.status == ChatMemberStatus.RESTRICTED
                    and not bool(getattr(member, "is_member", False))
                )
            )

        return await verify_raffle_members(user_ids, valid)

    async def finalize(self, raffle: Raffle, *, notify_admin: bool) -> None:
        async with self.chat_lock(raffle.chat_id):
            current = await self.storage.raffle(raffle.id)
            if current is None or current.status != "running":
                self.runtime.drop(raffle.chat_id, raffle_id=raffle.id)
                return
            candidates = await self._valid_entries(current)
            winner_total = min(current.winner_count, len(candidates))
            winners = tuple(secrets.SystemRandom().sample(candidates, winner_total))
            result_anchor_id = current.message_id
            if result_anchor_id is None:
                result_anchor_id = await self._create_group_result_anchor(current)
            if result_anchor_id is None:
                return
            admin_message_id = (
                await self._ensure_admin_result_placeholder(current) if notify_admin else None
            )
            delivery_stage = "group_and_admin" if admin_message_id is not None else "group_only"
            if not await self.storage.finish_raffle(
                current.id,
                winners,
                result_delivery_stage=delivery_stage,
                result_group_message_id=result_anchor_id,
                result_admin_message_id=admin_message_id,
            ):
                return
            self.runtime.drop(current.chat_id, raffle_id=current.id)
            pending = await self.storage.pending_raffle_results(
                chat_ids={current.chat_id},
                raffle_id=current.id,
            )
            first_round = next((item for item in pending if item.round_number == 1), None)
            if first_round is not None:
                await self._deliver_pending_result(first_round, winners=winners)

    @staticmethod
    def _admin_result_setting_key(raffle_id: int) -> str:
        return f"raffle_admin_result_message:{raffle_id}"

    async def _create_group_result_anchor(self, raffle: Raffle) -> int | None:
        try:
            with suspend_ephemeral_tracking():
                result_anchor = await self.bot.send_message(
                    raffle.chat_id,
                    RAFFLE_RESULT_ANCHOR_TEXT,
                )
        except TelegramAPIError:
            logger.warning(
                "创建抽奖结果消息锚点失败 chat_id=%s raffle_id=%s",
                raffle.chat_id,
                raffle.id,
            )
            return None
        message_id = getattr(result_anchor, "message_id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            logger.warning(
                "Telegram 未返回有效抽奖结果消息锚点 chat_id=%s raffle_id=%s",
                raffle.chat_id,
                raffle.id,
            )
            return None
        try:
            await self.storage.enqueue_ephemeral(
                raffle.chat_id,
                message_id,
                self._message_cleanup_deadline(),
            )
        except Exception:
            logger.exception(
                "抽奖结果消息锚点清理入队失败 chat_id=%s raffle_id=%s",
                raffle.chat_id,
                raffle.id,
            )
            with contextlib.suppress(TelegramAPIError):
                await self.bot.delete_message(raffle.chat_id, message_id)
            return None
        return message_id

    async def _ensure_admin_result_placeholder(self, raffle: Raffle) -> int | None:
        if len(self.settings.authorized_admin_ids) != 1:
            return None
        key = self._admin_result_setting_key(raffle.id)
        current = await self.storage.runtime_setting(raffle.chat_id, key)
        if current is not None:
            try:
                message_id = int(current)
            except ValueError:
                message_id = 0
            if message_id > 0:
                return message_id

        await self.storage.set_runtime_setting(raffle.chat_id, key, "creating")
        message_id = await self._send_admin_result_placeholder(raffle)
        if message_id is None:
            await self.storage.set_runtime_setting(raffle.chat_id, key, "disabled")
            return None
        await self.storage.set_runtime_setting(raffle.chat_id, key, str(message_id))
        return message_id

    async def _send_admin_result_placeholder(self, raffle: Raffle) -> int | None:
        if len(self.settings.authorized_admin_ids) != 1:
            return None
        admin_id = next(iter(self.settings.authorized_admin_ids))
        try:
            placeholder = await self.bot.send_message(
                admin_id,
                f"<b>🎟 抽奖 #{raffle.id}</b>\n奖品：{html.escape(raffle.prize)}\n状态：等待开奖。",
            )
        except TelegramAPIError:
            logger.debug("创建管理员抽奖结果锚点失败 raffle_id=%s", raffle.id)
            return None
        message_id = getattr(placeholder, "message_id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return None
        return message_id

    async def _replace_pending_result_anchor(
        self,
        pending: PendingRaffleResult,
        *,
        target: Literal["group", "admin"],
    ) -> PendingRaffleResult | None:
        if target == "group":
            message_id = await self._create_group_result_anchor(pending.raffle)
        else:
            message_id = await self._send_admin_result_placeholder(pending.raffle)
        if message_id is None:
            return None
        rebound: PendingRaffleResult | None = None
        try:
            rebound = await self.storage.bind_pending_raffle_result_anchor(
                pending,
                target=target,
                message_id=message_id,
            )
        except Exception:
            logger.exception(
                "抽奖结果消息锚点绑定失败 chat_id=%s raffle_id=%s target=%s",
                pending.raffle.chat_id,
                pending.raffle.id,
                target,
            )
        if rebound is None:
            if target == "admin" and len(self.settings.authorized_admin_ids) == 1:
                with contextlib.suppress(TelegramAPIError):
                    await self.bot.delete_message(
                        next(iter(self.settings.authorized_admin_ids)),
                        message_id,
                    )
            return None
        if target == "admin":
            try:
                await self.storage.set_runtime_setting(
                    pending.raffle.chat_id,
                    self._admin_result_setting_key(pending.raffle.id),
                    str(message_id),
                )
            except Exception:
                logger.exception(
                    "管理员抽奖结果锚点索引更新失败 raffle_id=%s",
                    pending.raffle.id,
                )
        return rebound

    async def _cancel_admin_result_placeholder(
        self,
        raffle: Raffle,
        *,
        text: str,
        remove_setting: bool,
    ) -> None:
        key = self._admin_result_setting_key(raffle.id)
        value = await self.storage.runtime_setting(raffle.chat_id, key)
        try:
            message_id = int(value or "")
        except ValueError:
            message_id = 0
        if message_id > 0 and len(self.settings.authorized_admin_ids) == 1:
            admin_id = next(iter(self.settings.authorized_admin_ids))
            try:
                await self.bot.edit_message_text(
                    html.escape(text),
                    chat_id=admin_id,
                    message_id=message_id,
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    logger.debug("更新管理员抽奖锚点失败 raffle_id=%s", raffle.id)
            except TelegramAPIError:
                logger.debug("更新管理员抽奖锚点失败 raffle_id=%s", raffle.id)
        if remove_setting:
            await self.storage.delete_runtime_setting(raffle.chat_id, key)

    async def _defer_pending_result(self, pending: PendingRaffleResult) -> bool:
        try:
            await self.storage.touch_pending_raffle_result(pending)
        except Exception:
            logger.exception(
                "抽奖结果重试时间更新失败 chat_id=%s raffle_id=%s",
                pending.raffle.chat_id,
                pending.raffle.id,
            )
        return False

    async def _deliver_pending_result(
        self,
        pending: PendingRaffleResult,
        *,
        winners: tuple[int, ...] | None = None,
    ) -> bool:
        raffle = pending.raffle
        current = pending
        if winners is None:
            winners = tuple(
                winner.user_id
                for winner in await self.storage.raffle_winners(
                    raffle.id,
                    latest_round=False,
                )
                if winner.round == pending.round_number
            )
        if current.stage in {"group_and_admin", "admin_only"} and (
            current.admin_message_id is None
        ):
            admin_message_id = await self._ensure_admin_result_placeholder(raffle)
            if admin_message_id is None:
                return await self._defer_pending_result(current)
            current = await self.storage.bind_pending_raffle_result_anchor(
                current,
                target="admin",
                message_id=admin_message_id,
            )
            if current is None:
                return await self._defer_pending_result(pending)
        if current.stage != "admin_only":
            if current.group_message_id is None:
                current = await self._replace_pending_result_anchor(
                    current,
                    target="group",
                )
                if current is None:
                    return await self._defer_pending_result(pending)
            if current.group_message_id is None:
                return await self._defer_pending_result(current)
            try:
                await self._publish_result(
                    raffle,
                    winners,
                    round_number=current.round_number,
                    message_id=current.group_message_id,
                )
            except _RaffleResultAnchorMissing:
                current = await self._replace_pending_result_anchor(
                    current,
                    target="group",
                )
                if current is None or current.group_message_id is None:
                    return await self._defer_pending_result(current or pending)
                try:
                    await self._publish_result(
                        raffle,
                        winners,
                        round_number=current.round_number,
                        message_id=current.group_message_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "抽奖群结果待重试 chat_id=%s raffle_id=%s error=%s",
                        raffle.chat_id,
                        raffle.id,
                        type(exc).__name__,
                    )
                    return await self._defer_pending_result(current)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "抽奖群结果待重试 chat_id=%s raffle_id=%s error=%s",
                    raffle.chat_id,
                    raffle.id,
                    type(exc).__name__,
                )
                return await self._defer_pending_result(current)
            if current.stage == "group_and_admin":
                try:
                    transitioned = await self.storage.update_pending_raffle_result(
                        current,
                        stage="admin_only",
                        group_delete_at=self._message_cleanup_deadline(),
                    )
                except Exception:
                    logger.exception(
                        "抽奖结果状态推进失败 chat_id=%s raffle_id=%s",
                        raffle.chat_id,
                        raffle.id,
                    )
                    return await self._defer_pending_result(current)
                if transitioned is None:
                    return await self._defer_pending_result(current)
                current = transitioned
            else:
                try:
                    return await self.storage.complete_pending_raffle_result(
                        current,
                        group_delete_at=self._message_cleanup_deadline(),
                    )
                except Exception:
                    logger.exception(
                        "抽奖结果完成标记失败 chat_id=%s raffle_id=%s",
                        raffle.chat_id,
                        raffle.id,
                    )
                    return await self._defer_pending_result(current)

        if current.admin_message_id is None:
            try:
                return await self.storage.complete_pending_raffle_result(current)
            except Exception:
                logger.exception(
                    "抽奖结果完成标记失败 chat_id=%s raffle_id=%s",
                    raffle.chat_id,
                    raffle.id,
                )
                return await self._defer_pending_result(current)
        try:
            delivered = await self._publish_admin_result(
                raffle,
                winners,
                round_number=current.round_number,
                message_id=current.admin_message_id,
            )
        except _RaffleResultAnchorMissing:
            current = await self._replace_pending_result_anchor(
                current,
                target="admin",
            )
            if current is None or current.admin_message_id is None:
                return await self._defer_pending_result(current or pending)
            try:
                delivered = await self._publish_admin_result(
                    raffle,
                    winners,
                    round_number=current.round_number,
                    message_id=current.admin_message_id,
                )
            except _RaffleResultAnchorMissing:
                return await self._defer_pending_result(current)
        if not delivered:
            return await self._defer_pending_result(current)
        try:
            return await self.storage.complete_pending_raffle_result(current)
        except Exception:
            logger.exception(
                "抽奖结果完成标记失败 chat_id=%s raffle_id=%s",
                raffle.chat_id,
                raffle.id,
            )
            return await self._defer_pending_result(current)

    async def _recover_pending_results(self, *, force: bool = False) -> None:
        updated_before = None
        if not force:
            updated_before = (
                datetime.now(UTC) - timedelta(seconds=RAFFLE_RESULT_RETRY_SECONDS)
            ).isoformat(timespec="seconds")
        blocked_raffles: set[int] = set()
        for pending in await self.storage.pending_raffle_results(
            chat_ids=self.settings.allowed_chat_ids,
            updated_before=updated_before,
        ):
            if pending.raffle.id in blocked_raffles:
                continue
            async with self.chat_lock(pending.raffle.chat_id):
                latest = await self.storage.pending_raffle_results(
                    chat_ids={pending.raffle.chat_id},
                    raffle_id=pending.raffle.id,
                )
                current = next(
                    (item for item in latest if item.round_number == pending.round_number),
                    None,
                )
                if current is None:
                    continue
                delivered = await self._deliver_pending_result(current)
                if not delivered:
                    blocked_raffles.add(pending.raffle.id)

    async def _publish_result(
        self,
        raffle: Raffle,
        winners: tuple[int, ...],
        *,
        round_number: int,
        message_id: int,
    ) -> None:
        await self._cancel_card_update(raffle.id)
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
        if not await self._unpin(raffle):
            raise RuntimeError("Telegram 未确认取消抽奖置顶")
        try:
            with suspend_ephemeral_tracking():
                await self.bot.edit_message_text(
                    result,
                    chat_id=raffle.chat_id,
                    message_id=message_id,
                    reply_markup=None,
                )
        except TelegramBadRequest as exc:
            detail = str(exc).casefold()
            if "message is not modified" in detail:
                pass
            elif "message not found" in detail or "message to edit not found" in detail:
                raise _RaffleResultAnchorMissing from exc
            else:
                raise
        if raffle.message_id is not None and raffle.message_id != message_id:
            await self._schedule_card_cleanup(raffle)

    async def _publish_admin_result(
        self,
        raffle: Raffle,
        winners: tuple[int, ...],
        *,
        round_number: int,
        message_id: int,
    ) -> bool:
        if len(self.settings.authorized_admin_ids) != 1:
            return True
        admin_id = next(iter(self.settings.authorized_admin_ids))
        ids = "、".join(str(user_id) for user_id in winners) if winners else "无"
        try:
            await self.bot.edit_message_text(
                f"抽奖 #{raffle.id} 第 {round_number} 轮完成。\n"
                f"奖品：{html.escape(raffle.prize)}\n中奖用户 ID：<code>{ids}</code>",
                chat_id=admin_id,
                message_id=message_id,
            )
        except TelegramBadRequest as exc:
            detail = str(exc).casefold()
            if "message is not modified" in detail:
                return True
            if "message not found" in detail or "message to edit not found" in detail:
                logger.warning("管理员抽奖结果锚点已不存在 raffle_id=%s", raffle.id)
                raise _RaffleResultAnchorMissing from exc
            logger.debug("私聊编辑抽奖结果失败 raffle_id=%s", raffle.id)
            return False
        except TelegramAPIError:
            logger.debug("私聊编辑抽奖结果失败 raffle_id=%s", raffle.id)
            return False
        return True

    async def cancel_active(self, chat_id: int) -> None:
        async with self.chat_lock(chat_id):
            raffle = await self.ensure_active(chat_id)
            if raffle is None:
                await self.bot.send_message(chat_id, "当前没有进行中的抽奖。")
                return
            if not await self.storage.cancel_raffle(raffle.id):
                return
            self.runtime.drop(chat_id, raffle_id=raffle.id)
            await self._cancel_card_update(raffle.id)
            text = f"<b>抽奖已取消</b>\n\n奖品：{html.escape(raffle.prize)}"
            await self._schedule_card_cleanup(raffle)
            await self._unpin(raffle)
            await self._cancel_admin_result_placeholder(
                raffle,
                text=f"抽奖 #{raffle.id} 已取消。",
                remove_setting=True,
            )
            if raffle.message_id is not None:
                try:
                    await self.bot.edit_message_text(
                        text,
                        chat_id=chat_id,
                        message_id=raffle.message_id,
                        reply_markup=None,
                    )
                    return
                except TelegramAPIError:
                    pass
            await self.bot.send_message(chat_id, text)

    async def _reroll_latest(self, chat_id: int) -> None:
        async with self.chat_lock(chat_id):
            raffle = await self.storage.latest_finished_raffle(chat_id)
            if raffle is None:
                await self.bot.send_message(chat_id, "没有可重抽的历史抽奖。")
                return
            pending_results = await self.storage.pending_raffle_results(
                chat_ids={chat_id},
                raffle_id=raffle.id,
            )
            if pending_results:
                for pending in pending_results:
                    if not await self._deliver_pending_result(pending):
                        break
                if await self.storage.pending_raffle_results(
                    chat_ids={chat_id},
                    raffle_id=raffle.id,
                ):
                    await self.bot.send_message(chat_id, "上一轮结果仍待发送，请稍后重试。")
                return
            candidates = list(await self._valid_entries(raffle))
            previous = {winner.user_id for winner in await self.storage.raffle_winners(raffle.id)}
            preferred = [user_id for user_id in candidates if user_id not in previous]
            pool = preferred if len(preferred) >= raffle.winner_count else candidates
            winners = tuple(
                secrets.SystemRandom().sample(
                    pool,
                    min(raffle.winner_count, len(pool)),
                )
            )
            result_anchor_id = await self._create_group_result_anchor(raffle)
            if result_anchor_id is None:
                return
            admin_message_id = await self._ensure_admin_result_placeholder(raffle)
            delivery_stage = "group_and_admin" if admin_message_id is not None else "group_only"
            round_number = await self.storage.add_reroll(
                raffle.id,
                winners,
                result_delivery_stage=delivery_stage,
                result_group_message_id=result_anchor_id,
                result_admin_message_id=admin_message_id,
            )
            pending = await self.storage.pending_raffle_results(
                chat_ids={chat_id},
                raffle_id=raffle.id,
            )
            current = next(
                (item for item in pending if item.round_number == round_number),
                None,
            )
            if current is not None:
                await self._deliver_pending_result(current, winners=winners)

    async def _run_worker(self) -> None:
        while True:
            try:
                await self._recover_pending_results()
                await self._reconcile_active_cards_if_due()
                due = await self.storage.due_raffles(
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    chat_ids=self.settings.allowed_chat_ids,
                )
                for raffle in due:
                    await self.finalize(raffle, notify_admin=True)
                await asyncio.sleep(self.worker_interval_seconds)
            except asyncio.CancelledError:
                if self._stopping:
                    raise
            except Exception:
                logger.exception("抽奖后台任务执行失败")
                await asyncio.sleep(self.worker_interval_seconds)
