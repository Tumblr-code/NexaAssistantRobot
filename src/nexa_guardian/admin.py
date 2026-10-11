from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdminRights:
    is_admin: bool
    can_delete_messages: bool
    can_restrict_members: bool


@dataclass(frozen=True, slots=True)
class _AdminCacheEntry:
    expires_at: float
    user_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class _RightsCacheEntry:
    expires_at: float
    rights: AdminRights


class AdminService:
    def __init__(
        self,
        bot: Bot,
        *,
        cache_seconds: int = 300,
        rights_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cache_seconds <= 0 or rights_timeout_seconds <= 0:
            raise ValueError("cache_seconds and rights_timeout_seconds must be positive")
        self.bot = bot
        self.cache_seconds = cache_seconds
        self.rights_timeout_seconds = rights_timeout_seconds
        self._clock = clock
        self._cache: dict[int, _AdminCacheEntry] = {}
        self._rights_cache: dict[tuple[int, int], _RightsCacheEntry] = {}
        self._refresh_tasks: dict[int, asyncio.Task[frozenset[int] | None]] = {}
        self._refresh_overrides: dict[int, dict[int, bool]] = {}

    async def cached_is_admin(self, chat_id: int, user_id: int) -> bool | None:
        """Return cached membership, retaining stale data across refresh failures."""
        now = self._clock()
        cached = self._cache.get(chat_id)
        if cached is None or cached.expires_at <= now:
            user_ids = await self._singleflight_refresh(chat_id)
            if user_ids is None:
                return None
            return user_id in user_ids
        return user_id in cached.user_ids

    async def warm(self, chat_ids: Iterable[int]) -> None:
        """Populate all configured chat caches without failing application startup."""
        unique_chat_ids = tuple(dict.fromkeys(chat_ids))
        if not unique_chat_ids:
            return
        await asyncio.gather(*(self._singleflight_refresh(chat_id) for chat_id in unique_chat_ids))

    def update_member(self, chat_id: int, user_id: int, *, is_admin: bool) -> None:
        """Apply one chat-member event to an existing or in-flight cache snapshot.

        Authorization configured outside this service must remain an independent
        exemption; this cache only reflects Telegram's current administrator set.
        """
        self._rights_cache.pop((chat_id, user_id), None)
        cached = self._cache.get(chat_id)
        refresh_task = self._refresh_tasks.get(chat_id)
        if cached is None or (refresh_task is not None and not refresh_task.done()):
            overrides = self._refresh_overrides.setdefault(chat_id, {})
            overrides[user_id] = is_admin
        if cached is None:
            return
        user_ids = set(cached.user_ids)
        if is_admin:
            user_ids.add(user_id)
        else:
            user_ids.discard(user_id)
        self._cache[chat_id] = _AdminCacheEntry(
            expires_at=cached.expires_at,
            user_ids=frozenset(user_ids),
        )

    async def _singleflight_refresh(self, chat_id: int) -> frozenset[int] | None:
        task = self._refresh_tasks.get(chat_id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._refresh(chat_id),
                name=f"nexa-admin-cache-refresh-{chat_id}",
            )
            self._refresh_tasks[chat_id] = task
            task.add_done_callback(
                lambda completed, target_chat_id=chat_id: self._refresh_done(
                    target_chat_id,
                    completed,
                )
            )
        return await asyncio.shield(task)

    async def _refresh(self, chat_id: int) -> frozenset[int] | None:
        stale = self._cache.get(chat_id)
        try:
            members = await self.bot.get_chat_administrators(chat_id, return_bots=False)
        except Exception:
            logger.exception("无法刷新群管理员缓存 chat_id=%s", chat_id)
            latest = self._cache.get(chat_id) or stale
            return latest.user_ids if latest is not None else None

        user_ids = {item.user.id for item in members}
        for key in tuple(self._rights_cache):
            if key[0] == chat_id:
                self._rights_cache.pop(key, None)
        expires_at = self._clock() + self.cache_seconds
        for member in members:
            rights = self._rights_from_member(member)
            if rights is not None:
                self._rights_cache[(chat_id, member.user.id)] = _RightsCacheEntry(
                    expires_at=expires_at,
                    rights=rights,
                )
        for user_id, is_admin in self._refresh_overrides.pop(chat_id, {}).items():
            if is_admin:
                user_ids.add(user_id)
            else:
                user_ids.discard(user_id)
        result = frozenset(user_ids)
        self._cache[chat_id] = _AdminCacheEntry(
            expires_at=expires_at,
            user_ids=result,
        )
        return result

    def _refresh_done(
        self,
        chat_id: int,
        task: asyncio.Task[frozenset[int] | None],
    ) -> None:
        if self._refresh_tasks.get(chat_id) is task:
            self._refresh_tasks.pop(chat_id, None)
        if task.cancelled():
            return
        # Retrieve unexpected failures even if every waiting caller was cancelled.
        exception = task.exception()
        if exception is not None:
            logger.error(
                "管理员缓存刷新任务异常 chat_id=%s",
                chat_id,
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    async def rights(self, chat_id: int, user_id: int) -> AdminRights:
        cached = self._rights_cache.get((chat_id, user_id))
        if cached is not None and cached.expires_at > self._clock():
            return cached.rights
        member = await asyncio.wait_for(
            self.bot.get_chat_member(chat_id, user_id),
            timeout=self.rights_timeout_seconds,
        )
        rights = self._rights_from_member(member) or AdminRights(False, False, False)
        self._rights_cache[(chat_id, user_id)] = _RightsCacheEntry(
            expires_at=self._clock() + self.cache_seconds,
            rights=rights,
        )
        return rights

    @staticmethod
    def _rights_from_member(member: object) -> AdminRights | None:
        status = getattr(member, "status", None)
        if isinstance(member, ChatMemberOwner) or status == ChatMemberStatus.CREATOR:
            return AdminRights(True, True, True)
        if isinstance(member, ChatMemberAdministrator):
            return AdminRights(
                is_admin=True,
                can_delete_messages=member.can_delete_messages,
                can_restrict_members=member.can_restrict_members,
            )
        return None

    async def unique_anonymous_actor(
        self, chat_id: int, authorized_admin_ids: frozenset[int]
    ) -> tuple[int, AdminRights] | None:
        """Resolve an anonymous command only when its human actor is unambiguous."""
        members = await self.bot.get_chat_administrators(chat_id)
        anonymous_humans = [
            member
            for member in members
            if not member.user.is_bot and getattr(member, "is_anonymous", False)
        ]
        if len(anonymous_humans) != 1:
            return None

        member = anonymous_humans[0]
        if authorized_admin_ids and member.user.id not in authorized_admin_ids:
            return None
        if isinstance(member, ChatMemberOwner) or member.status == ChatMemberStatus.CREATOR:
            return member.user.id, AdminRights(True, True, True)
        if isinstance(member, ChatMemberAdministrator):
            return member.user.id, AdminRights(
                is_admin=True,
                can_delete_messages=member.can_delete_messages,
                can_restrict_members=member.can_restrict_members,
            )
        return None

    async def target_is_admin(self, chat_id: int, user_id: int) -> bool:
        member = await self.bot.get_chat_member(chat_id, user_id)
        return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}

    def invalidate(self, chat_id: int) -> None:
        self._cache.pop(chat_id, None)
        for key in tuple(self._rights_cache):
            if key[0] == chat_id:
                self._rights_cache.pop(key, None)
