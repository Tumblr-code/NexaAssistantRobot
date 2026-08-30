from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from nexa_guardian.storage import Raffle, normalize_raffle_keyword

MAX_RAFFLE_VERIFICATION_WORKERS = 8


class ActiveRaffleStorage(Protocol):
    async def active_raffle(self, chat_id: int) -> Raffle | None: ...


@dataclass(frozen=True, slots=True)
class _CachedRaffle:
    raffle: Raffle
    ends_at: datetime


class RaffleRuntime:
    """In-memory active-raffle index and per-chat operation locks.

    Storage is consulted only by the explicit :meth:`sync` operation. Message
    filters use :meth:`keyword_match`, which performs no I/O and returns early
    before normalization when the group has no active raffle.
    """

    def __init__(
        self,
        storage: ActiveRaffleStorage,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._active: dict[int, _CachedRaffle] = {}
        self._chat_locks: dict[int, asyncio.Lock] = {}

    async def sync(self, chat_id: int) -> Raffle | None:
        """Refresh one group from persistent state.

        This is intended for startup and explicit recovery paths, not ordinary
        message filtering. Creation/cancellation paths should call set()/drop()
        while holding chat_lock().
        """

        target_chat_id = int(chat_id)
        async with self.chat_lock(target_chat_id):
            raffle = await self.storage.active_raffle(target_chat_id)
            if raffle is None or not self.set(raffle):
                self.drop(target_chat_id)
                return None
            return raffle

    def set(self, raffle: Raffle) -> bool:
        """Cache a running, published, unexpired raffle."""

        ends_at = self._as_utc(raffle.ends_at, field="raffle.ends_at")
        if (
            raffle.status != "running"
            or raffle.publication_state != "active"
            or raffle.normalized_keyword is None
            or ends_at <= self._now()
        ):
            self.drop(raffle.chat_id, raffle_id=raffle.id)
            return False
        self._active[raffle.chat_id] = _CachedRaffle(raffle=raffle, ends_at=ends_at)
        return True

    def drop(self, chat_id: int, *, raffle_id: int | None = None) -> Raffle | None:
        """Remove a cached raffle, optionally only if its ID still matches."""

        target_chat_id = int(chat_id)
        cached = self._active.get(target_chat_id)
        if cached is None or (raffle_id is not None and cached.raffle.id != raffle_id):
            return None
        self._active.pop(target_chat_id, None)
        return cached.raffle

    def get(self, chat_id: int) -> Raffle | None:
        """Return the current raffle, dropping it automatically after its deadline."""

        target_chat_id = int(chat_id)
        cached = self._active.get(target_chat_id)
        if cached is None:
            return None
        if cached.ends_at <= self._now():
            self._active.pop(target_chat_id, None)
            return None
        return cached.raffle

    def has_active(self, chat_id: int) -> bool:
        return self.get(chat_id) is not None

    def keyword_match(self, chat_id: int, text: str) -> Raffle | None:
        """Match an exact normalized keyword without touching persistent storage."""

        raffle = self.get(chat_id)
        if raffle is None:
            return None
        try:
            normalized = normalize_raffle_keyword(text)
        except ValueError:
            return None
        return raffle if normalized == raffle.normalized_keyword else None

    def chat_lock(self, chat_id: int) -> asyncio.Lock:
        """Return the stable operation lock for one group."""

        target_chat_id = int(chat_id)
        lock = self._chat_locks.get(target_chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[target_chat_id] = lock
        return lock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("raffle runtime clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _as_utc(value: str, *, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO 8601 datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return parsed.astimezone(timezone.utc)


async def verify_raffle_members(
    user_ids: Iterable[int],
    verify: Callable[[int], Awaitable[bool]],
) -> tuple[int, ...]:
    """Verify raffle entrants with at most eight workers and preserve input order.

    The implementation creates one task per worker rather than one task per
    participant, keeping task and semaphore overhead bounded for large raffles.
    The callback owns Telegram-specific error handling and returns whether the
    member remains eligible.
    """

    ordered_ids = tuple(int(user_id) for user_id in user_ids)
    if not ordered_ids:
        return ()

    next_index = 0
    accepted = [False] * len(ordered_ids)
    stop = asyncio.Event()
    errors: list[Exception] = []

    async def worker() -> None:
        nonlocal next_index
        while not stop.is_set():
            index = next_index
            next_index += 1
            if index >= len(ordered_ids):
                return
            try:
                accepted[index] = bool(await verify(ordered_ids[index]))
            except Exception as exc:
                errors.append(exc)
                stop.set()
                return

    worker_count = min(MAX_RAFFLE_VERIFICATION_WORKERS, len(ordered_ids))
    tasks = [
        asyncio.create_task(worker(), name=f"nexa-raffle-verify-{index}")
        for index in range(worker_count)
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if errors:
        raise errors[0]
    return tuple(user_id for index, user_id in enumerate(ordered_ids) if accepted[index])
