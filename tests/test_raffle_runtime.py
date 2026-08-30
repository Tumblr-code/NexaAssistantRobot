import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from nexa_guardian.raffle_runtime import (
    MAX_RAFFLE_VERIFICATION_WORKERS,
    RaffleRuntime,
    verify_raffle_members,
)
from nexa_guardian.storage import Raffle

UTC = timezone.utc
GROUP_ID = -100
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def raffle(*, raffle_id: int = 1, ends_at: datetime | None = None) -> Raffle:
    deadline = ends_at or NOW + timedelta(hours=1)
    return Raffle(
        id=raffle_id,
        chat_id=GROUP_ID,
        message_id=88,
        creator_id=7,
        keyword="NEXA",
        normalized_keyword="nexa",
        prize="Prize",
        winner_count=1,
        ends_at=deadline.isoformat(),
        status="running",
        publication_state="active",
        created_at=NOW.isoformat(),
        finished_at=None,
    )


def test_keyword_checks_without_active_raffle_do_not_query_storage() -> None:
    storage = AsyncMock()
    runtime = RaffleRuntime(storage, clock=lambda: NOW)

    assert runtime.get(GROUP_ID) is None
    assert not runtime.has_active(GROUP_ID)
    assert runtime.keyword_match(GROUP_ID, "NEXA") is None
    storage.active_raffle.assert_not_awaited()


async def test_sync_is_the_only_storage_read_and_enables_memory_matches() -> None:
    active = raffle()
    storage = AsyncMock()
    storage.active_raffle.return_value = active
    runtime = RaffleRuntime(storage, clock=lambda: NOW)

    assert await runtime.sync(GROUP_ID) == active
    assert runtime.keyword_match(GROUP_ID, " ＮＥＸＡ\u200b ") == active
    assert runtime.keyword_match(GROUP_ID, "other") is None
    assert runtime.has_active(GROUP_ID)
    storage.active_raffle.assert_awaited_once_with(GROUP_ID)


def test_expired_raffle_is_dropped_and_no_longer_matches() -> None:
    clock = {"now": NOW}
    storage = AsyncMock()
    runtime = RaffleRuntime(storage, clock=lambda: clock["now"])
    active = raffle(ends_at=NOW + timedelta(seconds=1))

    assert runtime.set(active)
    clock["now"] = NOW + timedelta(seconds=1)
    assert runtime.keyword_match(GROUP_ID, "NEXA") is None
    assert runtime.get(GROUP_ID) is None
    storage.active_raffle.assert_not_awaited()


def test_conditional_drop_does_not_remove_newer_raffle() -> None:
    storage = AsyncMock()
    runtime = RaffleRuntime(storage, clock=lambda: NOW)
    newer = raffle(raffle_id=2)
    assert runtime.set(newer)

    assert runtime.drop(GROUP_ID, raffle_id=1) is None
    assert runtime.get(GROUP_ID) == newer
    assert runtime.drop(GROUP_ID, raffle_id=2) == newer
    assert runtime.get(GROUP_ID) is None


def test_chat_locks_are_stable_and_isolated() -> None:
    runtime = RaffleRuntime(AsyncMock(), clock=lambda: NOW)

    assert runtime.chat_lock(GROUP_ID) is runtime.chat_lock(GROUP_ID)
    assert runtime.chat_lock(GROUP_ID) is not runtime.chat_lock(GROUP_ID - 1)


async def test_member_verification_uses_at_most_eight_workers_and_preserves_order() -> None:
    active = 0
    peak = 0
    all_workers_started = asyncio.Event()
    release = asyncio.Event()

    async def verify(user_id: int) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if peak == MAX_RAFFLE_VERIFICATION_WORKERS:
            all_workers_started.set()
        await release.wait()
        await asyncio.sleep((20 - user_id) / 10000)
        active -= 1
        return user_id % 2 == 0

    task = asyncio.create_task(verify_raffle_members(range(20), verify))
    await asyncio.wait_for(all_workers_started.wait(), timeout=1)
    assert peak == MAX_RAFFLE_VERIFICATION_WORKERS
    release.set()

    assert await task == tuple(range(0, 20, 2))
    assert peak == MAX_RAFFLE_VERIFICATION_WORKERS


async def test_member_verification_uses_only_needed_workers() -> None:
    calls: list[int] = []

    async def verify(user_id: int) -> bool:
        calls.append(user_id)
        return True

    assert await verify_raffle_members((9, 7, 5), verify) == (9, 7, 5)
    assert sorted(calls) == [5, 7, 9]


async def test_member_verification_propagates_callback_errors_without_leaking_tasks() -> None:
    async def verify(user_id: int) -> bool:
        if user_id == 2:
            raise RuntimeError("telegram failed")
        await asyncio.sleep(0)
        return True

    with pytest.raises(RuntimeError, match="telegram failed"):
        await verify_raffle_members(range(20), verify)
