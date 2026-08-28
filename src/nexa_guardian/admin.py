from __future__ import annotations

import logging
import time
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


class AdminService:
    def __init__(self, bot: Bot, *, cache_seconds: int = 300) -> None:
        self.bot = bot
        self.cache_seconds = cache_seconds
        self._cache: dict[int, tuple[float, frozenset[int]]] = {}

    async def cached_is_admin(self, chat_id: int, user_id: int) -> bool | None:
        """Return None when Telegram cannot be queried so filtering can fail open."""
        now = time.monotonic()
        cached = self._cache.get(chat_id)
        if cached is None or cached[0] <= now:
            try:
                members = await self.bot.get_chat_administrators(chat_id, return_bots=False)
            except Exception:
                logger.exception("无法刷新群管理员缓存 chat_id=%s", chat_id)
                return None
            cached = (now + self.cache_seconds, frozenset(item.user.id for item in members))
            self._cache[chat_id] = cached
        return user_id in cached[1]

    async def rights(self, chat_id: int, user_id: int) -> AdminRights:
        member = await self.bot.get_chat_member(chat_id, user_id)
        if isinstance(member, ChatMemberOwner) or member.status == ChatMemberStatus.CREATOR:
            return AdminRights(True, True, True)
        if isinstance(member, ChatMemberAdministrator):
            return AdminRights(
                is_admin=True,
                can_delete_messages=member.can_delete_messages,
                can_restrict_members=member.can_restrict_members,
            )
        return AdminRights(False, False, False)

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
