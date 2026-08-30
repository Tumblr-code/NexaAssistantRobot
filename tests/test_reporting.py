from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from nexa_guardian.reporting import ActionReporter
from nexa_guardian.storage import LogOutboxRecord

UTC = timezone.utc


async def test_log_outbox_honors_telegram_retry_after() -> None:
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=-200, text="log"),
        message="retry",
        retry_after=60,
    )
    storage = AsyncMock()
    reporter = ActionReporter(
        bot,
        storage,
        -200,
        clock=lambda: datetime(2026, 8, 31, tzinfo=UTC),
    )
    record = LogOutboxRecord(
        id=1,
        chat_id=-100,
        message_id=None,
        actor_id=7,
        target_id=42,
        action="member_cleanup",
        due_at="2026-08-31T00:00:00+00:00",
        attempt_count=0,
        created_at="2026-08-31T00:00:00+00:00",
    )

    await reporter._deliver(record)

    storage.reschedule_log_outbox.assert_awaited_once_with(
        1,
        "2026-08-31T00:01:00+00:00",
    )
    storage.ack_log_outbox.assert_not_awaited()
