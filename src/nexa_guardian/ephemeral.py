from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

logger = logging.getLogger(__name__)

MessageResult = TypeVar("MessageResult")


@dataclass(frozen=True, slots=True)
class EphemeralRecord:
    chat_id: int
    message_id: int
    delete_at: str


class EphemeralRecordLike(Protocol):
    @property
    def chat_id(self) -> int: ...

    @property
    def message_id(self) -> int: ...

    @property
    def delete_at(self) -> str: ...


class EphemeralStorage(Protocol):
    async def enqueue_ephemeral(
        self,
        chat_id: int,
        message_id: int,
        delete_at: str,
    ) -> None: ...

    async def due_ephemeral(self, now: str, limit: int = 100) -> Sequence[EphemeralRecordLike]: ...

    async def ack_ephemeral(self, chat_id: int, message_id: int) -> None: ...

    async def reschedule_ephemeral(
        self,
        chat_id: int,
        message_id: int,
        delete_at: str,
    ) -> None: ...


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ephemeral clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class EphemeralMessenger:
    """Send and remove bot messages without retaining their contents.

    The storage only receives Telegram identifiers and deletion timestamps. Retry
    counters remain in memory. Editing an already tracked message deliberately leaves
    its original deletion deadline unchanged.
    """

    def __init__(
        self,
        bot: Bot,
        storage: EphemeralStorage,
        *,
        ttl_seconds: float = 30,
        poll_interval: float = 1,
        batch_size: int = 100,
        retry_delay_seconds: float = 5,
        max_retry_delay_seconds: float = 300,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        if max_retry_delay_seconds < retry_delay_seconds:
            raise ValueError("max_retry_delay_seconds must not be shorter than retry delay")

        self.bot = bot
        self.storage = storage
        self.ttl_seconds = ttl_seconds
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.retry_delay_seconds = retry_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._worker_task: asyncio.Task[None] | None = None
        self._retry_attempts: dict[tuple[int, int], int] = {}
        self._fallback_deadlines: dict[tuple[int, int], str] = {}

    async def start(self) -> None:
        """Recover already-due messages before starting the polling worker."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        await self.process_due()
        self._worker_task = asyncio.create_task(
            self._run_worker(),
            name="nexa-ephemeral-message-worker",
        )

    async def shutdown(self) -> None:
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._flush_fallback(self._clock(), force_delete=True)

    async def track_sent(self, message: MessageResult) -> MessageResult:
        """Persist one sent message's deletion deadline and return it unchanged."""
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        message_id = getattr(message, "message_id", None)
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            raise TypeError("tracked message must expose integer chat.id and message_id")

        key = (chat_id, message_id)
        delete_at = self._fallback_deadlines.get(key)
        if delete_at is None:
            delete_at = _utc_iso(self._clock() + timedelta(seconds=self.ttl_seconds))
        try:
            await self.storage.enqueue_ephemeral(chat_id, message_id, delete_at)
        except Exception:
            logger.exception(
                "failed to persist ephemeral message; using memory fallback "
                "chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            self._fallback_deadlines.setdefault(key, delete_at)
        else:
            self._fallback_deadlines.pop(key, None)
        return message

    async def track(self, message: MessageResult) -> MessageResult:
        """Backward-compatible short alias for :meth:`track_sent`."""
        return await self.track_sent(message)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Message:
        sent = await self.bot.send_message(chat_id, text, **kwargs)
        return await self.track_sent(sent)

    async def answer(
        self,
        message: Message,
        text: str,
        **kwargs: Any,
    ) -> Message:
        sent = await message.answer(text, **kwargs)
        return await self.track_sent(sent)

    async def edit(
        self,
        message: Message,
        text: str,
        **kwargs: Any,
    ) -> Message | bool:
        """Edit an existing message without changing its tracked deadline."""
        return await message.edit_text(text, **kwargs)

    async def process_due(self) -> int:
        """Attempt one bounded batch and return the number of completed records."""
        now = self._clock()
        completed = await self._flush_fallback(now, force_delete=False)
        due = await self.storage.due_ephemeral(_utc_iso(now), self.batch_size)
        for record in due:
            key = (record.chat_id, record.message_id)
            try:
                deleted = await self.bot.delete_message(record.chat_id, record.message_id)
                if deleted is False:
                    raise RuntimeError("Telegram returned false while deleting a message")
            except asyncio.CancelledError:
                raise
            except (TelegramBadRequest, TelegramForbiddenError):
                await self.storage.ack_ephemeral(record.chat_id, record.message_id)
                self._retry_attempts.pop(key, None)
                completed += 1
            except Exception as exc:
                attempts = min(self._retry_attempts.get(key, 0) + 1, 31)
                self._retry_attempts[key] = attempts
                retry_delay = min(
                    self.max_retry_delay_seconds,
                    self.retry_delay_seconds * (2 ** (attempts - 1)),
                )
                retry_at = now + timedelta(seconds=retry_delay)
                logger.warning(
                    "ephemeral deletion failed; retry scheduled chat_id=%s "
                    "message_id=%s attempts=%s error=%s",
                    record.chat_id,
                    record.message_id,
                    attempts,
                    type(exc).__name__,
                )
                await self.storage.reschedule_ephemeral(
                    record.chat_id,
                    record.message_id,
                    _utc_iso(retry_at),
                )
            else:
                await self.storage.ack_ephemeral(record.chat_id, record.message_id)
                self._retry_attempts.pop(key, None)
                completed += 1
        return completed

    async def _flush_fallback(self, now: datetime, *, force_delete: bool) -> int:
        completed = 0
        now_iso = _utc_iso(now)
        for key, delete_at in tuple(self._fallback_deadlines.items()):
            chat_id, message_id = key
            try:
                await self.storage.enqueue_ephemeral(chat_id, message_id, delete_at)
            except Exception:
                if not force_delete and delete_at > now_iso:
                    continue
                try:
                    deleted = await self.bot.delete_message(chat_id, message_id)
                    if deleted is False:
                        raise RuntimeError("Telegram returned false while deleting a message")
                except asyncio.CancelledError:
                    raise
                except (TelegramBadRequest, TelegramForbiddenError):
                    self._fallback_deadlines.pop(key, None)
                    completed += 1
                except Exception as exc:
                    logger.warning(
                        "memory fallback deletion failed chat_id=%s message_id=%s error=%s",
                        chat_id,
                        message_id,
                        type(exc).__name__,
                    )
                else:
                    self._fallback_deadlines.pop(key, None)
                    completed += 1
            else:
                self._fallback_deadlines.pop(key, None)
        return completed

    async def _run_worker(self) -> None:
        while True:
            await self._sleep(self.poll_interval)
            try:
                await self.process_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ephemeral message worker iteration failed")


class EphemeralRequestMiddleware:
    """Track every Telegram API call that returns sent or edited messages.

    The database uses ``ON CONFLICT DO NOTHING``, so editing an already tracked
    message never extends its original deadline. Media groups return a list and
    each message in that list receives its own deadline.
    """

    def __init__(self, messenger: EphemeralMessenger) -> None:
        self.messenger = messenger

    async def __call__(
        self,
        make_request: Callable[[Bot, Any], Awaitable[Any]],
        bot: Bot,
        method: Any,
    ) -> Any:
        result = await make_request(bot, method)
        if isinstance(result, Message):
            messages = (result,)
        elif isinstance(result, (list, tuple)):
            messages = tuple(item for item in result if isinstance(item, Message))
        else:
            messages = ()
        for message in messages:
            try:
                await self.messenger.track_sent(message)
            except Exception:
                logger.exception(
                    "failed to queue ephemeral bot message chat_id=%s message_id=%s",
                    message.chat.id,
                    message.message_id,
                )
        return result
