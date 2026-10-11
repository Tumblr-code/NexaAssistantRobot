from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypeVar

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message

logger = logging.getLogger(__name__)

_TRACK_EPHEMERAL_MESSAGES: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "nexa_track_ephemeral_messages",
    default=True,
)

MessageResult = TypeVar("MessageResult")


class _DeleteRequestCancelledError(RuntimeError):
    """A child Telegram request was cancelled without cancelling its worker."""


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


def _message_is_already_absent(error: TelegramBadRequest) -> bool:
    """Return whether Telegram explicitly confirms that the message is gone."""
    return "message to delete not found" in str(error).casefold()


@contextlib.contextmanager
def suspend_ephemeral_tracking() -> Iterator[None]:
    """Keep one outgoing request out of the global temporary-message queue.

    The caller must durably enqueue or exempt the resulting message itself. A
    context variable keeps concurrent Telegram requests in other tasks covered
    by the normal middleware policy.
    """

    token = _TRACK_EPHEMERAL_MESSAGES.set(False)
    try:
        yield
    finally:
        _TRACK_EPHEMERAL_MESSAGES.reset(token)


class EphemeralMessenger:
    """Send and remove non-private bot messages without retaining their contents.

    The storage only receives Telegram identifiers and deletion timestamps. Retry
    counters remain in memory. Editing an already tracked message deliberately leaves
    its original deletion deadline unchanged. Private-chat responses are deliberately
    left persistent and never enter the deletion queue.
    """

    def __init__(
        self,
        bot: Bot,
        storage: EphemeralStorage,
        *,
        delete_bot: Bot | None = None,
        delete_timeout_seconds: float = 10,
        delete_concurrency: int = 4,
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
        if not 0 < delete_timeout_seconds < float("inf"):
            raise ValueError("delete_timeout_seconds must be finite and positive")
        if not 1 <= delete_concurrency <= 4:
            raise ValueError("delete_concurrency must be between one and four")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        if max_retry_delay_seconds < retry_delay_seconds:
            raise ValueError("max_retry_delay_seconds must not be shorter than retry delay")

        self.bot = bot
        self.delete_bot = delete_bot if delete_bot is not None else bot
        self.storage = storage
        self.delete_timeout_seconds = delete_timeout_seconds
        self.delete_concurrency = delete_concurrency
        self.ttl_seconds = ttl_seconds
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.retry_delay_seconds = retry_delay_seconds
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._worker_task: asyncio.Task[None] | None = None
        self._restart_handle: asyncio.TimerHandle | None = None
        self._stopping = False
        self._process_lock = asyncio.Lock()
        self._delete_semaphore = asyncio.Semaphore(delete_concurrency)
        self._inflight_deletions: set[tuple[int, int]] = set()
        self._retry_attempts: dict[tuple[int, int], int] = {}
        self._fallback_deadlines: dict[tuple[int, int], str] = {}

    async def start(self) -> None:
        """Start recovery in the background without delaying Telegram polling."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stopping = False
        if self._restart_handle is not None:
            self._restart_handle.cancel()
            self._restart_handle = None
        self._spawn_worker()

    def _spawn_worker(self) -> None:
        self._worker_task = asyncio.create_task(
            self._run_worker(),
            name="nexa-ephemeral-message-worker",
        )
        self._worker_task.add_done_callback(self._worker_done)

    async def shutdown(self) -> None:
        self._stopping = True
        if self._restart_handle is not None:
            self._restart_handle.cancel()
            self._restart_handle = None
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
        if getattr(chat, "type", None) == ChatType.PRIVATE:
            return message

        key = (chat_id, message_id)
        delete_at = self._fallback_deadlines.get(key)
        if delete_at is None:
            delete_at = _utc_iso(self._clock() + timedelta(seconds=self.ttl_seconds))
        # Register the in-memory fallback before awaiting SQLite. If the update
        # handler is cancelled after Telegram has sent the message, the worker can
        # still persist or delete it instead of losing the only cleanup record.
        self._fallback_deadlines.setdefault(key, delete_at)
        try:
            await self.storage.enqueue_ephemeral(chat_id, message_id, delete_at)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to persist ephemeral message; using memory fallback "
                "chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
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
        async with self._process_lock:
            now = self._clock()
            completed = await self._flush_fallback(now, force_delete=False)
            due = await self.storage.due_ephemeral(_utc_iso(now), self.batch_size)
            if not due:
                return completed
            outcomes = await asyncio.gather(*(self._process_record(record, now) for record in due))
            return completed + sum(outcomes)

    async def _process_record(self, record: EphemeralRecordLike, now: datetime) -> int:
        key = (record.chat_id, record.message_id)
        if key in self._inflight_deletions:
            return 0
        self._inflight_deletions.add(key)
        try:
            try:
                async with self._delete_semaphore:
                    deleted = await self._delete_message(record.chat_id, record.message_id)
                if deleted is False:
                    raise RuntimeError("Telegram returned false while deleting a message")
            except asyncio.CancelledError:
                raise
            except TelegramBadRequest as exc:
                if _message_is_already_absent(exc):
                    await self.storage.ack_ephemeral(record.chat_id, record.message_id)
                    self._retry_attempts.pop(key, None)
                    return 1
                return await self._reschedule_failure(record, key, now, exc)
            except TelegramForbiddenError as exc:
                return await self._reschedule_failure(record, key, now, exc)
            except Exception as exc:
                return await self._reschedule_failure(record, key, now, exc)
            else:
                await self.storage.ack_ephemeral(record.chat_id, record.message_id)
                self._retry_attempts.pop(key, None)
                return 1
        finally:
            self._inflight_deletions.discard(key)

    async def _reschedule_failure(
        self,
        record: EphemeralRecordLike,
        key: tuple[int, int],
        now: datetime,
        error: Exception,
    ) -> int:
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
            type(error).__name__,
        )
        await self.storage.reschedule_ephemeral(
            record.chat_id,
            record.message_id,
            _utc_iso(retry_at),
        )
        return 0

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
                    deleted = await self._delete_message(chat_id, message_id)
                    if deleted is False:
                        raise RuntimeError("Telegram returned false while deleting a message")
                except asyncio.CancelledError:
                    raise
                except TelegramBadRequest as exc:
                    if _message_is_already_absent(exc):
                        self._fallback_deadlines.pop(key, None)
                        completed += 1
                    else:
                        logger.warning(
                            "memory fallback deletion failed chat_id=%s message_id=%s error=%s",
                            chat_id,
                            message_id,
                            type(exc).__name__,
                        )
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

    async def _delete_message(self, chat_id: int, message_id: int) -> bool:
        """Delete through the isolated client without letting one request stall the worker."""
        delete_task = asyncio.create_task(
            self.delete_bot.delete_message(chat_id, message_id),
            name="nexa-ephemeral-delete-request",
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(delete_task),
                timeout=self.delete_timeout_seconds,
            )
        except asyncio.CancelledError as exc:
            if delete_task.cancelled():
                raise _DeleteRequestCancelledError(
                    "Telegram delete request cancelled independently"
                ) from exc
            self._cancel_delete_task(delete_task)
            raise
        except asyncio.TimeoutError:
            self._cancel_delete_task(delete_task)
            raise

    @staticmethod
    def _cancel_delete_task(task: asyncio.Task[bool]) -> None:
        """Request child cancellation without letting cleanup delay the worker."""
        task.cancel()

        def consume_result(completed: asyncio.Task[bool]) -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(consume_result)

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        if self._worker_task is task:
            self._worker_task = None
        if self._stopping:
            return
        if task.cancelled():
            logger.error("ephemeral message worker stopped unexpectedly after cancellation")
        else:
            exception = task.exception()
            if exception is None:
                logger.error("ephemeral message worker stopped unexpectedly without an error")
            else:
                logger.critical(
                    "ephemeral message worker stopped unexpectedly",
                    exc_info=(type(exception), exception, exception.__traceback__),
                )
        self._schedule_worker_restart()

    def _schedule_worker_restart(self) -> None:
        if self._stopping or self._restart_handle is not None:
            return
        loop = asyncio.get_running_loop()
        self._restart_handle = loop.call_later(
            self.poll_interval,
            self._restart_worker,
        )

    def _restart_worker(self) -> None:
        self._restart_handle = None
        if self._stopping:
            return
        if self._worker_task is not None and not self._worker_task.done():
            return
        logger.warning("restarting ephemeral message worker")
        self._spawn_worker()

    async def _run_worker(self) -> None:
        while True:
            try:
                await self.process_due()
                await self._sleep(self.poll_interval)
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                logger.error(
                    "ephemeral message worker received an unexpected cancellation; continuing"
                )
            except Exception:
                logger.exception("ephemeral message worker iteration failed")


class EphemeralRequestMiddleware:
    """Track non-private Telegram API calls that return sent or edited messages.

    The database uses ``ON CONFLICT DO NOTHING``, so editing an already tracked
    message never extends its original deadline. Media groups return a list and
    each non-private message in that list receives its own deadline.
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
        if not _TRACK_EPHEMERAL_MESSAGES.get():
            return result
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
