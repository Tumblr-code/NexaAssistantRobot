import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage, EditMessageText
from aiogram.types import Chat, Message

from nexa_guardian.admin import AdminService
from nexa_guardian.config import Settings
from nexa_guardian.raffle_service import RaffleService
from nexa_guardian.storage import Storage

UTC = timezone.utc
GROUP_ID = -100


def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="123:ABC",  # noqa: S106 - inert test credential
        allowed_chat_ids=frozenset({GROUP_ID}),
        authorized_admin_ids=frozenset({7}),
        log_chat_id=None,
        database_path=tmp_path / "guardian.db",
        allowed_domains=(),
        blocked_words=(),
        block_external_links=True,
        block_forwards=False,
        block_channel_mentions=False,
        warn_limit=3,
        notice_ttl_seconds=30,
        flood_window_seconds=8,
        flood_max_messages=6,
        duplicate_window_seconds=30,
        duplicate_max_messages=2,
        dry_run=False,
        log_level="INFO",
    )


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


def service(tmp_path: Path, storage: Storage, bot: AsyncMock | None = None) -> RaffleService:
    target_bot = bot or AsyncMock()
    return RaffleService(
        target_bot,
        settings(tmp_path),
        storage,
        AdminService(target_bot),
        worker_interval_seconds=3600,
        card_debounce_seconds=0,
    )


async def active_raffle(storage: Storage, *user_ids: int):
    raffle = await storage.create_raffle(
        chat_id=GROUP_ID,
        creator_id=7,
        keyword="参与",
        prize="GiftPass",
        winner_count=1,
        ends_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    assert await storage.set_raffle_message(raffle.id, 88)
    assert await storage.activate_raffle(raffle.id)
    for user_id in user_ids:
        assert (await storage.join_raffle(raffle.id, user_id))[0]
    current = await storage.active_raffle(GROUP_ID)
    assert current is not None
    return current


async def test_active_participants_reuse_cached_raffle(
    tmp_path: Path,
    storage: Storage,
) -> None:
    await active_raffle(storage, 41, 42)
    runtime_service = service(tmp_path, storage)
    lookup = AsyncMock(wraps=storage.active_raffle)
    storage.active_raffle = lookup  # type: ignore[method-assign]

    assert await runtime_service.active_participant_ids(GROUP_ID) == frozenset({41, 42})
    assert await runtime_service.active_participant_ids(GROUP_ID) == frozenset({41, 42})
    lookup.assert_awaited_once_with(GROUP_ID)


async def test_missing_card_is_recreated_with_compare_and_swap(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    bot = AsyncMock()
    bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(chat_id=GROUP_ID, message_id=88, text="card"),
        message="message to edit not found",
    )
    bot.send_message.return_value = Message(
        message_id=101,
        date=datetime.now(UTC),
        chat=Chat(id=GROUP_ID, type=ChatType.SUPERGROUP, title="Test"),
        text="replacement",
    )
    bot.pin_chat_message.return_value = True
    runtime_service = service(tmp_path, storage, bot)
    assert await runtime_service.runtime.sync(GROUP_ID) is not None
    replace_card = AsyncMock(wraps=storage.replace_active_raffle_message)
    storage.replace_active_raffle_message = replace_card  # type: ignore[method-assign]

    await runtime_service._edit_card(raffle, 1)

    replace_card.assert_awaited_once_with(raffle.id, 88, 101)
    current = await storage.raffle(raffle.id)
    assert current is not None and current.message_id == 101
    cached = runtime_service.runtime.get(GROUP_ID)
    assert cached is not None and cached.message_id == 101
    bot.pin_chat_message.assert_awaited_once_with(
        GROUP_ID,
        101,
        disable_notification=True,
    )


async def test_periodic_reconciliation_recovers_card_deleted_after_startup(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    clock = {"now": 0.0}
    bot = AsyncMock()
    bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(chat_id=GROUP_ID, message_id=88, text="card"),
        message="message to edit not found",
    )
    bot.send_message.return_value = Message(
        message_id=102,
        date=datetime.now(UTC),
        chat=Chat(id=GROUP_ID, type=ChatType.SUPERGROUP, title="Test"),
        text="replacement",
    )
    bot.pin_chat_message.return_value = True
    runtime_service = RaffleService(
        bot,
        settings(tmp_path),
        storage,
        AdminService(bot),
        worker_interval_seconds=3600,
        card_debounce_seconds=0,
        card_reconcile_seconds=60,
        monotonic=lambda: clock["now"],
    )
    assert await runtime_service.runtime.sync(GROUP_ID) is not None

    clock["now"] = 61
    await runtime_service._reconcile_active_cards_if_due()
    await runtime_service.flush_card_updates()

    current = await storage.raffle(raffle.id)
    assert current is not None and current.message_id == 102


async def test_cancel_cancels_pending_reconciliation_card_update(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    bot = AsyncMock()
    bot.unpin_chat_message.return_value = True
    runtime_service = RaffleService(
        bot,
        settings(tmp_path),
        storage,
        AdminService(bot),
        worker_interval_seconds=3600,
        card_debounce_seconds=3600,
    )

    count_started = asyncio.Event()
    release_count = asyncio.Event()
    original_count = storage.raffle_entry_count

    async def delayed_count(raffle_id: int) -> int:
        count_started.set()
        await release_count.wait()
        return await original_count(raffle_id)

    storage.raffle_entry_count = delayed_count  # type: ignore[method-assign]
    sync_task = asyncio.create_task(runtime_service._sync_active_cards())
    await count_started.wait()
    cancel_task = asyncio.create_task(runtime_service.cancel_active(GROUP_ID))
    await asyncio.sleep(0)
    assert not cancel_task.done()
    release_count.set()
    await asyncio.gather(sync_task, cancel_task)

    assert runtime_service._card_tasks == {}
    assert runtime_service._card_updates == {}
    current = await storage.raffle(raffle.id)
    assert current is not None and current.status == "cancelled"
    assert bot.edit_message_text.await_count == 1
    assert "抽奖已取消" in bot.edit_message_text.await_args.args[0]


async def test_startup_cleans_pending_cards_and_shutdown_stops_worker(
    tmp_path: Path,
    storage: Storage,
) -> None:
    pending = await storage.create_raffle(
        chat_id=GROUP_ID,
        creator_id=7,
        keyword="参与",
        prize="GiftPass",
        winner_count=1,
        ends_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    assert await storage.set_raffle_message(pending.id, 88)
    bot = AsyncMock()
    bot.unpin_chat_message.return_value = True
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service.startup()
    assert runtime_service._worker_task is not None
    assert await storage.pending_raffles() == ()
    bot.unpin_chat_message.assert_awaited_once_with(GROUP_ID, message_id=88)
    bot.delete_message.assert_awaited_once_with(GROUP_ID, 88)

    await runtime_service.shutdown()
    assert runtime_service._worker_task is None


async def test_pending_cleanup_is_scoped_to_allowed_chat(
    tmp_path: Path,
    storage: Storage,
) -> None:
    allowed = await storage.create_raffle(
        chat_id=GROUP_ID,
        creator_id=7,
        keyword="参与",
        prize="GiftPass",
        winner_count=1,
        ends_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    foreign = await storage.create_raffle(
        chat_id=-200,
        creator_id=8,
        keyword="foreign",
        prize="Other",
        winner_count=1,
        ends_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    assert await storage.set_raffle_message(allowed.id, 88)
    assert await storage.set_raffle_message(foreign.id, 99)
    bot = AsyncMock()
    bot.unpin_chat_message.return_value = True
    bot.delete_message.return_value = True
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service._clean_pending()

    assert await storage.raffle(allowed.id) is None
    assert await storage.raffle(foreign.id) is not None
    bot.delete_message.assert_awaited_once_with(GROUP_ID, 88)


async def test_failed_orphan_card_delete_stays_in_ephemeral_queue(
    tmp_path: Path,
    storage: Storage,
) -> None:
    pending = await storage.create_raffle(
        chat_id=GROUP_ID,
        creator_id=7,
        keyword="参与",
        prize="GiftPass",
        winner_count=1,
        ends_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    assert await storage.set_raffle_message(pending.id, 88)
    bot = AsyncMock()
    bot.unpin_chat_message.return_value = True
    bot.delete_message.side_effect = TelegramBadRequest(
        method=DeleteMessage(chat_id=GROUP_ID, message_id=88),
        message="temporary deletion failure",
    )
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service._clean_pending()

    queued = await storage.due_ephemeral("2100-01-01T00:00:00+00:00")
    assert [(item.chat_id, item.message_id) for item in queued] == [(GROUP_ID, 88)]


async def test_expired_running_raffle_still_blocks_destructive_operations(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await storage.create_raffle(
        chat_id=GROUP_ID,
        creator_id=7,
        keyword="参与",
        prize="GiftPass",
        winner_count=1,
        ends_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    assert await storage.set_raffle_message(raffle.id, 88)
    assert await storage.activate_raffle(raffle.id)
    runtime_service = service(tmp_path, storage)

    active = await runtime_service.ensure_active(GROUP_ID)

    assert active is not None and active.id == raffle.id
    assert runtime_service.runtime.get(GROUP_ID) is None


async def test_finished_result_is_retried_from_persistent_marker(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.MEMBER,
        user=SimpleNamespace(is_bot=False),
    )
    bot.unpin_chat_message.return_value = True
    bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(chat_id=GROUP_ID, message_id=88, text="result"),
        message="temporary edit failure",
    )
    bot.send_message.return_value = SimpleNamespace(message_id=99)
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service.finalize(raffle, notify_admin=False)

    current = await storage.raffle(raffle.id)
    assert current is not None and current.status == "finished"
    pending = await storage.pending_raffle_results(chat_ids={GROUP_ID})
    assert [(item.raffle.id, item.round_number, item.stage) for item in pending] == [
        (raffle.id, 1, "group_only")
    ]
    assert pending[0].group_message_id == 88
    assert bot.send_message.await_count == 0

    bot.edit_message_text.side_effect = None
    bot.send_message.side_effect = None
    await runtime_service._reroll_latest(GROUP_ID)

    assert await storage.pending_raffle_results(chat_ids={GROUP_ID}) == ()
    assert bot.send_message.await_count == 0
    assert [call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list] == [
        88,
        88,
    ]


async def test_missing_group_result_anchor_is_replaced_without_sending_result_twice(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.MEMBER,
        user=SimpleNamespace(is_bot=False),
    )
    bot.unpin_chat_message.return_value = True
    bot.send_message.return_value = SimpleNamespace(message_id=99)

    async def edit_result(_text: str, *, message_id: int, **_kwargs: object) -> object:
        if message_id == 88:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=GROUP_ID, message_id=88, text="result"),
                message="message to edit not found",
            )
        return SimpleNamespace(message_id=message_id)

    bot.edit_message_text.side_effect = edit_result
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service.finalize(raffle, notify_admin=False)

    assert await storage.pending_raffle_results(chat_ids={GROUP_ID}) == ()
    assert [call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list] == [
        88,
        99,
    ]
    assert [call.args[1] for call in bot.send_message.await_args_list] == [
        "🎲 正在生成抽奖结果…",
    ]
    queued = await storage.due_ephemeral("2100-01-01T00:00:00+00:00")
    assert {(item.chat_id, item.message_id) for item in queued} == {
        (GROUP_ID, 88),
        (GROUP_ID, 99),
    }


async def test_missing_admin_result_anchor_is_replaced_before_completing_marker(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.MEMBER,
        user=SimpleNamespace(is_bot=False),
    )
    bot.unpin_chat_message.return_value = True
    bot.send_message.side_effect = (
        SimpleNamespace(message_id=99),
        SimpleNamespace(message_id=100),
    )

    async def edit_result(
        _text: str,
        *,
        chat_id: int,
        message_id: int,
        **_kwargs: object,
    ) -> object:
        if chat_id == 7 and message_id == 99:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=7, message_id=99, text="result"),
                message="message to edit not found",
            )
        return SimpleNamespace(message_id=message_id)

    bot.edit_message_text.side_effect = edit_result
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service.finalize(raffle, notify_admin=True)

    assert await storage.pending_raffle_results(chat_ids={GROUP_ID}) == ()
    assert [call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list] == [
        88,
        99,
        100,
    ]
    assert (
        await storage.runtime_setting(
            GROUP_ID,
            f"raffle_admin_result_message:{raffle.id}",
        )
        == "100"
    )


async def test_legacy_pending_result_creates_missing_admin_anchor(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    assert await storage.finish_raffle(
        raffle.id,
        (41,),
        result_delivery_stage="group_and_admin",
    )
    pending = (await storage.pending_raffle_results(chat_ids={GROUP_ID}))[0]
    assert pending.group_message_id == 88
    assert pending.admin_message_id is None
    bot = AsyncMock()
    bot.unpin_chat_message.return_value = True
    bot.send_message.return_value = SimpleNamespace(message_id=100)
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service._recover_pending_results(force=True)

    assert await storage.pending_raffle_results(chat_ids={GROUP_ID}) == ()
    assert [call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list] == [
        88,
        100,
    ]


async def test_admin_delivery_retry_does_not_repeat_group_result(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.MEMBER,
        user=SimpleNamespace(is_bot=False),
    )
    bot.unpin_chat_message.return_value = True

    bot.send_message.side_effect = (
        SimpleNamespace(message_id=99),
        SimpleNamespace(message_id=100),
    )

    async def fail_admin(_text: str, *, chat_id: int, **_kwargs: object) -> object:
        if chat_id == 7:
            raise TelegramBadRequest(
                method=DeleteMessage(chat_id=7, message_id=1),
                message="temporary private failure",
            )
        return SimpleNamespace(message_id=99)

    bot.edit_message_text.side_effect = fail_admin
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service.finalize(raffle, notify_admin=True)

    pending = await storage.pending_raffle_results(chat_ids={GROUP_ID})
    assert [(item.raffle.id, item.round_number, item.stage) for item in pending] == [
        (raffle.id, 1, "admin_only")
    ]
    assert bot.edit_message_text.await_count == 2
    assert (
        sum(call.kwargs["chat_id"] == GROUP_ID for call in bot.edit_message_text.await_args_list)
        == 1
    )

    bot.send_message.side_effect = None
    bot.edit_message_text.side_effect = None
    await runtime_service._recover_pending_results(force=True)

    assert await storage.pending_raffle_results(chat_ids={GROUP_ID}) == ()
    assert bot.edit_message_text.await_count == 3
    assert (
        sum(call.kwargs["chat_id"] == GROUP_ID for call in bot.edit_message_text.await_args_list)
        == 1
    )


async def test_group_delivery_failure_waits_before_editing_admin_result(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.MEMBER,
        user=SimpleNamespace(is_bot=False),
    )
    bot.unpin_chat_message.return_value = False
    bot.send_message.side_effect = (
        SimpleNamespace(message_id=99),
        SimpleNamespace(message_id=100),
    )
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service.finalize(raffle, notify_admin=True)

    pending = await storage.pending_raffle_results(chat_ids={GROUP_ID})
    assert [(item.round_number, item.stage) for item in pending] == [(1, "group_and_admin")]
    bot.edit_message_text.assert_not_awaited()

    bot.unpin_chat_message.return_value = True
    await runtime_service._recover_pending_results(force=True)

    assert await storage.pending_raffle_results(chat_ids={GROUP_ID}) == ()
    assert bot.edit_message_text.await_count == 2
    assert [call.kwargs["chat_id"] for call in bot.edit_message_text.await_args_list] == [
        GROUP_ID,
        7,
    ]


async def test_result_recovery_reuses_fixed_message_anchors_after_marker_failure(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.MEMBER,
        user=SimpleNamespace(is_bot=False),
    )
    bot.unpin_chat_message.return_value = True
    bot.send_message.side_effect = (
        SimpleNamespace(message_id=99),
        SimpleNamespace(message_id=100),
    )
    runtime_service = service(tmp_path, storage, bot)
    original_complete = storage.complete_pending_raffle_result
    complete_calls = 0

    async def fail_first_complete(pending):  # type: ignore[no-untyped-def]
        nonlocal complete_calls
        complete_calls += 1
        if complete_calls == 1:
            return False
        return await original_complete(pending)

    storage.complete_pending_raffle_result = fail_first_complete  # type: ignore[method-assign]

    await runtime_service.finalize(raffle, notify_admin=True)

    pending = await storage.pending_raffle_results(chat_ids={GROUP_ID})
    assert [(item.round_number, item.stage) for item in pending] == [(1, "admin_only")]
    assert bot.send_message.await_count == 1
    assert [call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list] == [
        88,
        99,
    ]

    await runtime_service._recover_pending_results(force=True)

    assert await storage.pending_raffle_results(chat_ids={GROUP_ID}) == ()
    assert bot.send_message.await_count == 1
    assert [call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list] == [
        88,
        99,
        99,
    ]


async def test_reroll_result_is_recovered_without_creating_another_round(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    assert await storage.finish_raffle(raffle.id, (41,))
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(
        status=ChatMemberStatus.MEMBER,
        user=SimpleNamespace(is_bot=False),
    )
    bot.unpin_chat_message.return_value = True
    bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(chat_id=GROUP_ID, message_id=88, text="result"),
        message="temporary edit failure",
    )
    bot.send_message.side_effect = (
        SimpleNamespace(message_id=99),
        SimpleNamespace(message_id=100),
    )
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service._reroll_latest(GROUP_ID)

    pending = await storage.pending_raffle_results(chat_ids={GROUP_ID})
    assert [(item.round_number, item.stage) for item in pending] == [(2, "group_and_admin")]
    winners = await storage.raffle_winners(raffle.id, latest_round=False)
    assert [winner.round for winner in winners] == [1, 2]

    bot.edit_message_text.side_effect = None
    bot.send_message.side_effect = None
    await runtime_service._reroll_latest(GROUP_ID)

    assert await storage.pending_raffle_results(chat_ids={GROUP_ID}) == ()
    winners = await storage.raffle_winners(raffle.id, latest_round=False)
    assert [winner.round for winner in winners] == [1, 2]


async def test_recovery_does_not_publish_later_round_after_earlier_failure(
    tmp_path: Path,
    storage: Storage,
) -> None:
    raffle = await active_raffle(storage, 41)
    assert await storage.finish_raffle(
        raffle.id,
        (41,),
        result_delivery_stage="group_only",
    )
    assert (
        await storage.add_reroll(
            raffle.id,
            (41,),
            result_delivery_stage="group_only",
        )
        == 2
    )
    bot = AsyncMock()
    bot.unpin_chat_message.return_value = False
    runtime_service = service(tmp_path, storage, bot)

    await runtime_service._recover_pending_results(force=True)

    assert bot.unpin_chat_message.await_count == 1
    pending = await storage.pending_raffle_results(chat_ids={GROUP_ID})
    assert [item.round_number for item in pending] == [1, 2]
