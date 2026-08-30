from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import DeleteMessage
from aiogram.types import Chat, Message

from nexa_guardian.ephemeral import (
    EphemeralMessenger,
    EphemeralRecord,
    EphemeralRequestMiddleware,
)

UTC = timezone.utc


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeStorage:
    def __init__(self, records: list[EphemeralRecord] | None = None) -> None:
        self.records = {(record.chat_id, record.message_id): record for record in (records or [])}
        self.enqueued: list[EphemeralRecord] = []
        self.acked: list[tuple[int, int]] = []
        self.rescheduled: list[EphemeralRecord] = []
        self.enqueue_error: Exception | None = None

    async def enqueue_ephemeral(
        self,
        chat_id: int,
        message_id: int,
        delete_at: str,
    ) -> None:
        if self.enqueue_error is not None:
            raise self.enqueue_error
        if (chat_id, message_id) in self.records:
            return
        record = EphemeralRecord(chat_id, message_id, delete_at)
        self.records[(chat_id, message_id)] = record
        self.enqueued.append(record)

    async def due_ephemeral(self, now: str, limit: int = 100) -> list[EphemeralRecord]:
        return sorted(
            (record for record in self.records.values() if record.delete_at <= now),
            key=lambda record: record.delete_at,
        )[:limit]

    async def ack_ephemeral(self, chat_id: int, message_id: int) -> None:
        self.records.pop((chat_id, message_id), None)
        self.acked.append((chat_id, message_id))

    async def reschedule_ephemeral(
        self,
        chat_id: int,
        message_id: int,
        delete_at: str,
    ) -> None:
        record = EphemeralRecord(chat_id, message_id, delete_at)
        self.records[(chat_id, message_id)] = record
        self.rescheduled.append(record)


class FakeSentMessage:
    def __init__(self, chat_id: int, message_id: int) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.message_id = message_id
        self.edits: list[tuple[str, dict[str, Any]]] = []

    async def edit_text(self, text: str, **kwargs: Any) -> FakeSentMessage:
        self.edits.append((text, kwargs))
        return self


class FakeIncomingMessage:
    def __init__(self, sent: FakeSentMessage) -> None:
        self.sent = sent
        self.answers: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: str, **kwargs: Any) -> FakeSentMessage:
        self.answers.append((text, kwargs))
        return self.sent


class FakeBot:
    def __init__(self) -> None:
        self.next_message_id = 100
        self.sent: list[tuple[int, str, dict[str, Any]]] = []
        self.deleted: list[tuple[int, int]] = []
        self.delete_results: list[bool | Exception] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> FakeSentMessage:
        self.sent.append((chat_id, text, kwargs))
        self.next_message_id += 1
        return FakeSentMessage(chat_id, self.next_message_id)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        if self.delete_results:
            result = self.delete_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return True


def at(seconds: int) -> datetime:
    return datetime(2026, 8, 29, 0, 0, seconds, tzinfo=timezone.utc)


def test_delete_concurrency_cannot_exceed_four() -> None:
    with pytest.raises(ValueError, match="between one and four"):
        EphemeralMessenger(  # type: ignore[arg-type]
            FakeBot(),
            FakeStorage(),
            delete_concurrency=5,
        )


async def test_send_and_answer_are_tracked_for_thirty_seconds() -> None:
    clock = MutableClock(at(0))
    storage = FakeStorage()
    bot = FakeBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        bot,
        storage,
        clock=clock,
    )

    sent = await messenger.send_message(-100, "temporary", disable_notification=True)
    answered = FakeSentMessage(-200, 22)
    incoming = FakeIncomingMessage(answered)
    result = await messenger.answer(incoming, "reply", reply_to_message_id=7)

    assert sent.message_id == 101
    assert result is answered
    assert bot.sent == [(-100, "temporary", {"disable_notification": True})]
    assert incoming.answers == [("reply", {"reply_to_message_id": 7})]
    assert [(item.chat_id, item.message_id) for item in storage.enqueued] == [
        (-100, 101),
        (-200, 22),
    ]
    assert {item.delete_at for item in storage.enqueued} == {"2026-08-29T00:00:30.000000+00:00"}


async def test_edit_does_not_reset_original_deadline() -> None:
    clock = MutableClock(at(0))
    storage = FakeStorage()
    bot = FakeBot()
    messenger = EphemeralMessenger(bot, storage, clock=clock)  # type: ignore[arg-type]
    sent = await messenger.send_message(-100, "reviewing")
    original_deadline = storage.enqueued[0].delete_at

    clock.advance(20)
    result = await messenger.edit(sent, "review complete", reply_markup=None)

    assert result is sent
    assert sent.edits == [("review complete", {"reply_markup": None})]
    assert len(storage.enqueued) == 1
    assert storage.records[(-100, 101)].delete_at == original_deadline


async def test_process_due_deletes_and_acknowledges_without_waiting() -> None:
    clock = MutableClock(at(30))
    record = EphemeralRecord(-100, 9, "2026-08-29T00:00:29.000000+00:00")
    storage = FakeStorage([record])
    bot = FakeBot()
    messenger = EphemeralMessenger(bot, storage, clock=clock)  # type: ignore[arg-type]

    completed = await messenger.process_due()

    assert completed == 1
    assert bot.deleted == [(-100, 9)]
    assert storage.acked == [(-100, 9)]
    assert storage.records == {}


async def test_process_due_uses_dedicated_delete_bot() -> None:
    clock = MutableClock(at(30))
    record = EphemeralRecord(-100, 9, "2026-08-29T00:00:29.000000+00:00")
    storage = FakeStorage([record])
    polling_bot = FakeBot()
    delete_bot = FakeBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        polling_bot,
        storage,
        delete_bot=delete_bot,  # type: ignore[arg-type]
        clock=clock,
    )

    assert await messenger.process_due() == 1

    assert polling_bot.deleted == []
    assert delete_bot.deleted == [(-100, 9)]
    assert storage.acked == [(-100, 9)]


async def test_start_does_not_block_polling_while_history_recovery_is_slow() -> None:
    records = [
        EphemeralRecord(-100, message_id, "2026-08-29T00:00:00.000000+00:00")
        for message_id in range(1, 101)
    ]
    storage = FakeStorage(records)
    four_deletes_started = asyncio.Event()

    class BlockingDeleteBot(FakeBot):
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            self.deleted.append((chat_id, message_id))
            if len(self.deleted) == 4:
                four_deletes_started.set()
            await asyncio.Event().wait()
            return True

    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        storage,
        delete_bot=BlockingDeleteBot(),  # type: ignore[arg-type]
        clock=MutableClock(at(1)),
    )

    await asyncio.wait_for(messenger.start(), timeout=0.05)
    polling_can_start = True
    await asyncio.wait_for(four_deletes_started.wait(), timeout=0.2)

    assert polling_can_start
    assert messenger._worker_task is not None
    assert not messenger._worker_task.done()

    await asyncio.wait_for(messenger.shutdown(), timeout=0.2)


async def test_due_deletions_are_bounded_to_four_concurrent_requests() -> None:
    records = [
        EphemeralRecord(-100, message_id, "2026-08-29T00:00:00.000000+00:00")
        for message_id in range(1, 101)
    ]
    storage = FakeStorage(records)

    class MeasuringDeleteBot(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            self.deleted.append((chat_id, message_id))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.005)
                return True
            finally:
                self.active -= 1

    delete_bot = MeasuringDeleteBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        storage,
        delete_bot=delete_bot,  # type: ignore[arg-type]
        clock=MutableClock(at(1)),
    )

    assert await messenger.process_due() == 100

    assert delete_bot.max_active == 4
    assert len(delete_bot.deleted) == 100
    assert len(storage.acked) == 100


async def test_concurrent_due_scans_do_not_delete_the_same_record_twice() -> None:
    record = EphemeralRecord(-100, 9, "2026-08-29T00:00:00.000000+00:00")
    storage = FakeStorage([record])
    delete_started = asyncio.Event()
    allow_delete = asyncio.Event()

    class BlockingDeleteBot(FakeBot):
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            self.deleted.append((chat_id, message_id))
            delete_started.set()
            await allow_delete.wait()
            return True

    delete_bot = BlockingDeleteBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        storage,
        delete_bot=delete_bot,  # type: ignore[arg-type]
        clock=MutableClock(at(1)),
    )

    first = asyncio.create_task(messenger.process_due())
    await delete_started.wait()
    second = asyncio.create_task(messenger.process_due())
    allow_delete.set()

    assert await asyncio.gather(first, second) == [1, 0]
    assert delete_bot.deleted == [(-100, 9)]


async def test_delete_timeout_reschedules_due_message() -> None:
    class BlockingDeleteBot(FakeBot):
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            self.deleted.append((chat_id, message_id))
            await asyncio.Event().wait()
            return True

    record = EphemeralRecord(-100, 10, "2026-08-29T00:00:29.000000+00:00")
    storage = FakeStorage([record])
    delete_bot = BlockingDeleteBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        storage,
        delete_bot=delete_bot,  # type: ignore[arg-type]
        delete_timeout_seconds=0.01,
        retry_delay_seconds=2,
        clock=MutableClock(at(30)),
    )

    assert await messenger.process_due() == 0

    assert delete_bot.deleted == [(-100, 10)]
    assert storage.acked == []
    assert storage.rescheduled == [EphemeralRecord(-100, 10, "2026-08-29T00:00:32.000000+00:00")]


async def test_child_delete_cancellation_is_logged_and_rescheduled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class SelfCancellingDeleteBot(FakeBot):
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            self.deleted.append((chat_id, message_id))
            raise asyncio.CancelledError

    record = EphemeralRecord(-100, 11, "2026-08-29T00:00:29.000000+00:00")
    storage = FakeStorage([record])
    delete_bot = SelfCancellingDeleteBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        storage,
        delete_bot=delete_bot,  # type: ignore[arg-type]
        retry_delay_seconds=2,
        clock=MutableClock(at(30)),
    )

    with caplog.at_level(logging.WARNING):
        assert await messenger.process_due() == 0

    assert delete_bot.deleted == [(-100, 11)]
    assert storage.acked == []
    assert storage.rescheduled == [EphemeralRecord(-100, 11, "2026-08-29T00:00:32.000000+00:00")]
    assert "ephemeral deletion failed; retry scheduled" in caplog.text
    assert "error=_DeleteRequestCancelledError" in caplog.text


async def test_shutdown_cancels_worker_during_delete_request() -> None:
    clock = MutableClock(at(0))
    record = EphemeralRecord(-100, 12, "2026-08-29T00:00:01.000000+00:00")
    storage = FakeStorage([record])
    delete_started = asyncio.Event()

    class BlockingDeleteBot(FakeBot):
        async def delete_message(self, chat_id: int, message_id: int) -> bool:
            self.deleted.append((chat_id, message_id))
            delete_started.set()
            await asyncio.Event().wait()
            return True

    async def advance_to_due(_: float) -> None:
        clock.advance(2)

    delete_bot = BlockingDeleteBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        storage,
        delete_bot=delete_bot,  # type: ignore[arg-type]
        clock=clock,
        sleep=advance_to_due,
    )

    await messenger.start()
    await asyncio.wait_for(delete_started.wait(), timeout=0.2)
    await asyncio.wait_for(messenger.shutdown(), timeout=0.2)

    assert messenger._worker_task is None
    assert delete_bot.deleted == [(-100, 12)]
    assert storage.acked == []
    assert storage.rescheduled == []
    assert (-100, 12) in storage.records


async def test_worker_logs_unexpected_cancellation_and_keeps_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleep_calls = 0
    worker_continued = asyncio.Event()

    async def cancel_once_then_block(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            raise asyncio.CancelledError
        worker_continued.set()
        await asyncio.Event().wait()

    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        FakeStorage(),
        clock=MutableClock(at(0)),
        sleep=cancel_once_then_block,
    )

    with caplog.at_level(logging.ERROR):
        await messenger.start()
        await asyncio.wait_for(worker_continued.wait(), timeout=0.2)

    assert messenger._worker_task is not None
    assert not messenger._worker_task.done()
    assert "worker received an unexpected cancellation; continuing" in caplog.text

    await asyncio.wait_for(messenger.shutdown(), timeout=0.2)


async def test_missing_or_forbidden_message_is_acknowledged() -> None:
    records = [
        EphemeralRecord(-100, 1, "2026-08-29T00:00:00.000000+00:00"),
        EphemeralRecord(-100, 2, "2026-08-29T00:00:00.000000+00:00"),
    ]
    storage = FakeStorage(records)
    bot = FakeBot()
    bot.delete_results = [
        TelegramBadRequest(
            method=DeleteMessage(chat_id=-100, message_id=1),
            message="message to delete not found",
        ),
        TelegramForbiddenError(
            method=DeleteMessage(chat_id=-100, message_id=2),
            message="bot was blocked",
        ),
    ]
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        bot,
        storage,
        clock=MutableClock(at(1)),
    )

    completed = await messenger.process_due()

    assert completed == 2
    assert storage.acked == [(-100, 1), (-100, 2)]
    assert storage.rescheduled == []


async def test_transient_errors_keep_retrying_without_blocking_other_records() -> None:
    records = [
        EphemeralRecord(-100, 1, "2026-08-29T00:00:00.000000+00:00"),
        EphemeralRecord(-100, 2, "2026-08-29T00:00:00.000000+00:00"),
    ]
    clock = MutableClock(at(1))
    storage = FakeStorage(records)
    bot = FakeBot()
    bot.delete_results = [RuntimeError("network"), True]
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        bot,
        storage,
        clock=clock,
        retry_delay_seconds=2,
        max_retry_delay_seconds=10,
    )

    completed = await messenger.process_due()

    assert completed == 1
    assert storage.acked == [(-100, 2)]
    assert len(storage.rescheduled) == 1
    assert storage.rescheduled[0].delete_at == "2026-08-29T00:00:03.000000+00:00"

    clock.advance(2)
    bot.delete_results = [RuntimeError("network")]
    completed = await messenger.process_due()

    assert completed == 0
    assert storage.acked == [(-100, 2)]
    assert (-100, 1) in storage.records
    assert storage.rescheduled[-1].delete_at == "2026-08-29T00:00:07.000000+00:00"


async def test_enqueue_failure_uses_memory_fallback_and_deletes_when_due() -> None:
    clock = MutableClock(at(0))
    storage = FakeStorage()
    storage.enqueue_error = RuntimeError("database unavailable")
    bot = FakeBot()
    messenger = EphemeralMessenger(bot, storage, clock=clock)  # type: ignore[arg-type]
    sent = FakeSentMessage(-100, 33)

    assert await messenger.track_sent(sent) is sent
    assert messenger._fallback_deadlines == {(-100, 33): "2026-08-29T00:00:30.000000+00:00"}

    clock.advance(30)
    completed = await messenger.process_due()

    assert completed == 1
    assert bot.deleted == [(-100, 33)]
    assert messenger._fallback_deadlines == {}


async def test_edit_after_enqueue_recovery_persists_original_fallback_deadline() -> None:
    clock = MutableClock(at(0))
    storage = FakeStorage()
    storage.enqueue_error = RuntimeError("database unavailable")
    bot = FakeBot()
    messenger = EphemeralMessenger(bot, storage, clock=clock)  # type: ignore[arg-type]
    sent = Message(
        message_id=77,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        text="正在审核…",
    )
    await messenger.track_sent(sent)
    original_deadline = messenger._fallback_deadlines[(-100, 77)]

    clock.advance(20)
    storage.enqueue_error = None
    middleware = EphemeralRequestMiddleware(messenger)
    await middleware(AsyncMock(return_value=sent), bot, object())  # type: ignore[arg-type]

    assert storage.records[(-100, 77)].delete_at == original_deadline
    assert messenger._fallback_deadlines == {}
    clock.advance(10)
    assert await messenger.process_due() == 1
    assert bot.deleted == [(-100, 77)]


async def test_shutdown_persists_memory_fallback_before_session_closes() -> None:
    storage = FakeStorage()
    storage.enqueue_error = RuntimeError("database unavailable")
    bot = FakeBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        bot,
        storage,
        clock=MutableClock(at(0)),
    )
    await messenger.track_sent(FakeSentMessage(42, 34))
    storage.enqueue_error = None

    await messenger.shutdown()

    assert (42, 34) in storage.records
    assert bot.deleted == []
    assert messenger._fallback_deadlines == {}


async def test_start_recovers_due_records_and_shutdown_cancels_worker() -> None:
    record = EphemeralRecord(-100, 7, "2026-08-29T00:00:00.000000+00:00")
    storage = FakeStorage([record])
    bot = FakeBot()
    sleep_started = asyncio.Event()

    async def sleeping(_: float) -> None:
        sleep_started.set()
        await asyncio.Event().wait()

    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        bot,
        storage,
        clock=MutableClock(at(1)),
        sleep=sleeping,
    )

    await messenger.start()
    await sleep_started.wait()

    assert bot.deleted == [(-100, 7)]
    assert storage.acked == [(-100, 7)]

    await messenger.shutdown()
    assert messenger._worker_task is None


async def test_track_rejects_non_message_results_without_storing_content() -> None:
    storage = FakeStorage()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        storage,
        clock=MutableClock(at(0)),
    )

    try:
        await messenger.track(True)
    except TypeError as exc:
        assert "chat.id" in str(exc)
    else:
        raise AssertionError("track should reject a non-message Telegram result")

    assert storage.enqueued == []


async def test_request_middleware_tracks_every_message_result() -> None:
    sent = Message(
        message_id=77,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        text="temporary",
    )
    messenger = SimpleNamespace(track_sent=AsyncMock(return_value=sent))
    middleware = EphemeralRequestMiddleware(messenger)  # type: ignore[arg-type]
    make_request = AsyncMock(return_value=sent)

    result = await middleware(make_request, FakeBot(), object())  # type: ignore[arg-type]

    assert result is sent
    messenger.track_sent.assert_awaited_once_with(sent)


async def test_private_message_is_not_queued_for_deletion() -> None:
    storage = FakeStorage()
    bot = FakeBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        bot,
        storage,
        clock=MutableClock(at(0)),
    )
    sent = Message(
        message_id=78,
        date=datetime.now(UTC),
        chat=Chat(id=42, type=ChatType.PRIVATE, first_name="Admin"),
        text="persistent private reply",
    )
    middleware = EphemeralRequestMiddleware(messenger)

    result = await middleware(AsyncMock(return_value=sent), bot, object())  # type: ignore[arg-type]

    assert result is sent
    assert storage.records == {}
    assert storage.enqueued == []


async def test_group_message_is_still_queued_for_deletion() -> None:
    storage = FakeStorage()
    bot = FakeBot()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        bot,
        storage,
        clock=MutableClock(at(0)),
    )
    sent = Message(
        message_id=79,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        text="temporary group reply",
    )
    middleware = EphemeralRequestMiddleware(messenger)

    result = await middleware(AsyncMock(return_value=sent), bot, object())  # type: ignore[arg-type]

    assert result is sent
    assert storage.records[(-100, 79)].delete_at == "2026-08-29T00:00:30.000000+00:00"


async def test_request_middleware_tracks_each_media_group_message() -> None:
    messages = [
        Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
            caption=f"image {message_id}",
        )
        for message_id in (80, 81)
    ]
    messenger = SimpleNamespace(track_sent=AsyncMock())
    middleware = EphemeralRequestMiddleware(messenger)  # type: ignore[arg-type]
    make_request = AsyncMock(return_value=messages)

    result = await middleware(make_request, FakeBot(), object())  # type: ignore[arg-type]

    assert result is messages
    assert messenger.track_sent.await_args_list == [
        call(messages[0]),
        call(messages[1]),
    ]


async def test_request_middleware_edit_does_not_extend_existing_deadline() -> None:
    clock = MutableClock(at(0))
    storage = FakeStorage()
    messenger = EphemeralMessenger(  # type: ignore[arg-type]
        FakeBot(),
        storage,
        clock=clock,
    )
    sent = Message(
        message_id=77,
        date=datetime.now(UTC),
        chat=Chat(id=-100, type=ChatType.SUPERGROUP, title="Test"),
        text="reviewing",
    )
    await messenger.track_sent(sent)
    original_deadline = storage.records[(-100, 77)].delete_at
    clock.advance(20)
    middleware = EphemeralRequestMiddleware(messenger)

    result = await middleware(AsyncMock(return_value=sent), FakeBot(), object())  # type: ignore[arg-type]

    assert result is sent
    assert storage.records[(-100, 77)].delete_at == original_deadline
    assert len(storage.enqueued) == 1
