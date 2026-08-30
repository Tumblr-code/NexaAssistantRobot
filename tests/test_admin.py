from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nexa_guardian.admin import AdminService


def member(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(id=user_id))


class MutableClock:
    def __init__(self) -> None:
        self.current = 0.0

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


async def test_one_hundred_concurrent_checks_share_one_refresh() -> None:
    refresh_started = asyncio.Event()
    allow_refresh = asyncio.Event()

    class Bot:
        calls = 0

        async def get_chat_administrators(
            self, chat_id: int, *, return_bots: bool
        ) -> list[SimpleNamespace]:
            assert chat_id == -100
            assert return_bots is False
            self.calls += 1
            refresh_started.set()
            await allow_refresh.wait()
            return [member(7)]

    bot = Bot()
    service = AdminService(bot)  # type: ignore[arg-type]
    checks = [asyncio.create_task(service.cached_is_admin(-100, 7)) for _ in range(100)]
    await refresh_started.wait()
    allow_refresh.set()

    assert await asyncio.gather(*checks) == [True] * 100
    assert bot.calls == 1


async def test_expired_cache_is_used_when_refresh_fails() -> None:
    clock = MutableClock()

    class Bot:
        calls = 0

        async def get_chat_administrators(
            self, chat_id: int, *, return_bots: bool
        ) -> list[SimpleNamespace]:
            self.calls += 1
            if self.calls == 1:
                return [member(7)]
            raise RuntimeError("telegram unavailable")

    bot = Bot()
    service = AdminService(bot, cache_seconds=10, clock=clock)  # type: ignore[arg-type]
    assert await service.cached_is_admin(-100, 7) is True
    clock.advance(11)

    assert await service.cached_is_admin(-100, 7) is True
    assert bot.calls == 2


async def test_warm_deduplicates_chat_ids_and_populates_cache() -> None:
    class Bot:
        calls = 0

        async def get_chat_administrators(
            self, chat_id: int, *, return_bots: bool
        ) -> list[SimpleNamespace]:
            self.calls += 1
            return [member(7)]

    bot = Bot()
    service = AdminService(bot)  # type: ignore[arg-type]

    await service.warm([-100, -100])

    assert await service.cached_is_admin(-100, 7) is True
    assert bot.calls == 1


async def test_member_updates_are_applied_during_and_after_refresh() -> None:
    refresh_started = asyncio.Event()
    allow_refresh = asyncio.Event()

    class Bot:
        async def get_chat_administrators(
            self, chat_id: int, *, return_bots: bool
        ) -> list[SimpleNamespace]:
            refresh_started.set()
            await allow_refresh.wait()
            return [member(7)]

    service = AdminService(Bot())  # type: ignore[arg-type]
    pending = asyncio.create_task(service.cached_is_admin(-100, 8))
    await refresh_started.wait()
    service.update_member(-100, 8, is_admin=True)
    allow_refresh.set()

    assert await pending is True
    service.update_member(-100, 7, is_admin=False)
    assert await service.cached_is_admin(-100, 7) is False
    assert await service.cached_is_admin(-100, 8) is True
