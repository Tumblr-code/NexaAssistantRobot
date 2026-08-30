from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError


class HistoryPurgeError(RuntimeError):
    pass


class TelegramAdminSessionError(RuntimeError):
    pass


class TelegramAdminSession:
    """Serialize and verify access to the pre-authorized Telegram user session."""

    def __init__(
        self,
        *,
        api_id: int | None,
        api_hash: str,
        session_path: Path,
        authorized_user_ids: frozenset[int],
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.authorized_user_ids = authorized_user_ids
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.api_id is not None and bool(self.api_hash)

    @asynccontextmanager
    async def connect(self, chat_id: int) -> AsyncIterator[tuple[Any, Any]]:
        if not self.configured:
            raise TelegramAdminSessionError("尚未配置 Telegram API 凭据。")
        if self._lock.locked():
            raise TelegramAdminSessionError("已有 MTProto 管理任务正在运行。")
        async with self._lock:
            assert self.api_id is not None
            self.session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            client = TelegramClient(
                str(self.session_path),
                self.api_id,
                self.api_hash,
                flood_sleep_threshold=30,
            )
            await client.connect()
            try:
                if not await client.is_user_authorized():
                    raise TelegramAdminSessionError("管理员 Telegram 用户会话尚未登录。")
                identity: Any = await client.get_me()
                if identity is None or identity.id not in self.authorized_user_ids:
                    raise TelegramAdminSessionError("Telegram 用户会话不是唯一授权管理员账号。")
                entity: Any = await client.get_entity(chat_id)
                yield client, entity
            finally:
                disconnect_result: Any = client.disconnect()
                if inspect.isawaitable(disconnect_result):
                    await disconnect_result


@dataclass(frozen=True, slots=True)
class HistoryPurgeResult:
    deleted: int
    skipped: int
    failed: int


class HistoryPurger:
    """Delete supergroup history through one pre-authorized Telegram user session."""

    def __init__(
        self,
        *,
        api_id: int | None,
        api_hash: str,
        session_path: Path,
        authorized_user_ids: frozenset[int],
        session: TelegramAdminSession | None = None,
    ) -> None:
        self.session = session or TelegramAdminSession(
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
            authorized_user_ids=authorized_user_ids,
        )

    @property
    def configured(self) -> bool:
        return self.session.configured

    async def purge(self, chat_id: int) -> HistoryPurgeResult:
        try:
            async with self.session.connect(chat_id) as (client, entity):
                deleted = skipped = failed = 0
                for _scan in range(3):
                    message_ids = [message.id async for message in client.iter_messages(entity)]
                    if not message_ids:
                        break
                    for offset in range(0, len(message_ids), 100):
                        batch = message_ids[offset : offset + 100]
                        try:
                            await self._delete_with_flood_retry(client, entity, batch)
                        except RPCError:
                            for message_id in batch:
                                try:
                                    await self._delete_with_flood_retry(
                                        client, entity, [message_id]
                                    )
                                except RPCError:
                                    skipped += 1
                                except Exception:
                                    failed += 1
                                else:
                                    deleted += 1
                        else:
                            deleted += len(batch)
                return HistoryPurgeResult(deleted=deleted, skipped=skipped, failed=failed)
        except TelegramAdminSessionError as exc:
            message = str(exc)
            if "尚未配置" in message:
                message = "全群清理尚未配置 Telegram API 凭据。"
            raise HistoryPurgeError(message) from exc

    @staticmethod
    async def _delete_with_flood_retry(
        client: TelegramClient, entity: Any, message_ids: list[int]
    ) -> None:
        try:
            await client.delete_messages(entity, message_ids)
        except FloodWaitError as exc:
            if exc.seconds > 300:
                raise HistoryPurgeError("Telegram 限流超过 300 秒，清理已中止。") from exc
            await asyncio.sleep(exc.seconds + 1)
            await client.delete_messages(entity, message_ids)
