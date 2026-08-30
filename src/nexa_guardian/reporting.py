from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from nexa_guardian.storage import LogOutboxRecord, Storage

logger = logging.getLogger(__name__)
UTC = timezone.utc
LOG_DELIVERY_CONCURRENCY = 4


class ActionReporter:
    """Persist moderation actions and deliver optional audit notifications."""

    def __init__(
        self,
        bot: Bot,
        storage: Storage,
        log_chat_id: int | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.log_chat_id = log_chat_id
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._delivery_semaphore = asyncio.Semaphore(LOG_DELIVERY_CONCURRENCY)
        self._clock = clock or (lambda: datetime.now(UTC))

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
            async with self._delivery_semaphore:
                await self.bot.send_message(self.log_chat_id, text)
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            delay = max(float(exc.retry_after), 2 ** min(record.attempt_count + 1, 8))
            due_at = (self._clock() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            await self.storage.reschedule_log_outbox(record.id, due_at)
            logger.warning(
                "群管日志投递受限，已按 Telegram 等待时间重排 "
                "log_chat_id=%s outbox_id=%s retry_after=%s",
                self.log_chat_id,
                record.id,
                exc.retry_after,
            )
        except Exception:
            delay = min(300, 2 ** min(record.attempt_count + 1, 8))
            due_at = (self._clock() + timedelta(seconds=delay)).isoformat(timespec="seconds")
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
