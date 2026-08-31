from __future__ import annotations

import asyncio
import inspect
import logging
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError

logger = logging.getLogger(__name__)

SESSION_SETUP_TIMEOUT_SECONDS = 15.0
HISTORY_PURGE_MAX_FLOOD_WAIT_SECONDS = 300
HISTORY_PURGE_BATCH_SIZE = 100
SAFE_PROBE_DETAILS = frozenset(
    {
        "MTProto 管理会话必须绑定唯一授权管理员。",
        "MTProto 管理会话必须绑定唯一目标群。",
        "拒绝访问非目标群的 MTProto 管理任务。",
        "管理员 Telegram 用户会话文件不存在。",
        "管理员 Telegram 用户会话必须是普通的非符号链接文件。",
        "管理员 Telegram 用户会话文件权限不得宽于 0600。",
        "管理员 Telegram 用户会话尚未登录。",
        "Telegram 用户会话不是唯一授权管理员账号。",
        "MTProto 管理会话连接验证超过 15 秒。",
    }
)


class HistoryPurgeError(RuntimeError):
    pass


class TelegramAdminSessionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionHealth:
    configured: bool
    verified: bool
    detail: str


class TelegramAdminSession:
    """Serialize and verify access to the pre-authorized Telegram user session."""

    def __init__(
        self,
        *,
        api_id: int | None,
        api_hash: str,
        session_path: Path,
        authorized_user_ids: frozenset[int],
        allowed_chat_ids: frozenset[int],
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.authorized_user_ids = authorized_user_ids
        self.allowed_chat_ids = allowed_chat_ids
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.api_id is not None and bool(self.api_hash)

    @property
    def session_file(self) -> Path:
        path = self.session_path
        if str(path).endswith(".session"):
            return path
        return Path(f"{path}.session")

    def _validate_session_file(self) -> None:
        path = self.session_file
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise TelegramAdminSessionError("管理员 Telegram 用户会话文件不存在。") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise TelegramAdminSessionError("管理员 Telegram 用户会话必须是普通的非符号链接文件。")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & ~0o600:
            raise TelegramAdminSessionError("管理员 Telegram 用户会话文件权限不得宽于 0600。")

    async def _setup(self, client: TelegramClient, chat_id: int) -> tuple[Any, Any]:
        await client.connect()
        if not await client.is_user_authorized():
            raise TelegramAdminSessionError("管理员 Telegram 用户会话尚未登录。")
        identity: Any = await client.get_me()
        if identity is None or identity.id not in self.authorized_user_ids:
            raise TelegramAdminSessionError("Telegram 用户会话不是唯一授权管理员账号。")
        entity: Any = await client.get_entity(chat_id)
        return identity, entity

    async def probe(self, chat_id: int) -> SessionHealth:
        """Verify the configured user identity and target group without managing either."""
        if not self.configured:
            return SessionHealth(
                configured=False,
                verified=False,
                detail="未配置 Telegram API 凭据",
            )
        if self._lock.locked():
            return SessionHealth(configured=True, verified=False, detail="任务运行中")
        try:
            async with self.connect(chat_id):
                pass
        except TelegramAdminSessionError as exc:
            detail = str(exc)
            if "已有 MTProto" in detail:
                detail = "任务运行中"
            elif detail not in SAFE_PROBE_DETAILS:
                detail = "身份或目标群验证失败"
            return SessionHealth(configured=True, verified=False, detail=detail)
        except Exception:
            return SessionHealth(configured=True, verified=False, detail="身份或目标群验证失败")
        return SessionHealth(
            configured=True,
            verified=True,
            detail="身份与目标群已验证",
        )

    @asynccontextmanager
    async def connect(self, chat_id: int) -> AsyncIterator[tuple[Any, Any]]:
        if not self.configured:
            raise TelegramAdminSessionError("尚未配置 Telegram API 凭据。")
        if len(self.authorized_user_ids) != 1:
            raise TelegramAdminSessionError("MTProto 管理会话必须绑定唯一授权管理员。")
        if len(self.allowed_chat_ids) != 1:
            raise TelegramAdminSessionError("MTProto 管理会话必须绑定唯一目标群。")
        if chat_id not in self.allowed_chat_ids:
            raise TelegramAdminSessionError("拒绝访问非目标群的 MTProto 管理任务。")
        if self._lock.locked():
            raise TelegramAdminSessionError("已有 MTProto 管理任务正在运行。")
        async with self._lock:
            assert self.api_id is not None
            self._validate_session_file()
            client = TelegramClient(
                str(self.session_path),
                self.api_id,
                self.api_hash,
                flood_sleep_threshold=0,
            )
            try:
                try:
                    _, entity = await asyncio.wait_for(
                        self._setup(client, chat_id),
                        timeout=SESSION_SETUP_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    raise TelegramAdminSessionError("MTProto 管理会话连接验证超过 15 秒。") from exc
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
        allowed_chat_ids: frozenset[int],
        session: TelegramAdminSession | None = None,
    ) -> None:
        self.session = session or TelegramAdminSession(
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
            authorized_user_ids=authorized_user_ids,
            allowed_chat_ids=allowed_chat_ids,
        )

    @property
    def configured(self) -> bool:
        return self.session.configured

    async def purge(self, chat_id: int) -> HistoryPurgeResult:
        try:
            async with self.session.connect(chat_id) as (client, entity):
                deleted = skipped = failed = 0
                flood_wait_used = [False]
                lower_id = 0
                for _scan in range(3):
                    upper_id = await self._latest_message_id_with_flood_retry(
                        client,
                        entity,
                        flood_wait_used=flood_wait_used,
                    )
                    if upper_id <= lower_id:
                        break
                    scan_deleted, scan_skipped, scan_failed = await self._purge_range(
                        client,
                        entity,
                        lower_id=lower_id,
                        upper_id=upper_id,
                        flood_wait_used=flood_wait_used,
                    )
                    deleted += scan_deleted
                    skipped += scan_skipped
                    failed += scan_failed
                    lower_id = upper_id
                return HistoryPurgeResult(deleted=deleted, skipped=skipped, failed=failed)
        except asyncio.CancelledError:
            raise
        except TelegramAdminSessionError as exc:
            message = str(exc)
            if "尚未配置" in message:
                message = "全群清理尚未配置 Telegram API 凭据。"
            raise HistoryPurgeError(message) from exc
        except HistoryPurgeError:
            raise
        except Exception as exc:
            logger.exception("Telegram 群历史清理失败 chat_id=%s", chat_id)
            raise HistoryPurgeError("Telegram 群历史清理失败。") from exc

    @staticmethod
    async def _latest_message_id_with_flood_retry(
        client: TelegramClient,
        entity: Any,
        *,
        flood_wait_used: list[bool],
    ) -> int:
        async def collect() -> int:
            ids = [
                message.id
                async for message in client.iter_messages(
                    entity,
                    limit=1,
                )
            ]
            return int(ids[0]) if ids else 0

        try:
            return await collect()
        except FloodWaitError as exc:
            await HistoryPurger._wait_for_flood(exc, flood_wait_used=flood_wait_used)
        try:
            return await collect()
        except FloodWaitError as exc:
            raise HistoryPurgeError("Telegram 再次限流，清理已中止。") from exc

    @staticmethod
    async def _purge_range(
        client: TelegramClient,
        entity: Any,
        *,
        lower_id: int,
        upper_id: int,
        flood_wait_used: list[bool],
    ) -> tuple[int, int, int]:
        deleted = skipped = failed = 0
        resume_max_id = upper_id + 1
        while resume_max_id > lower_id + 1:
            batch: list[int] = []
            try:
                async for message in client.iter_messages(
                    entity,
                    min_id=lower_id,
                    max_id=resume_max_id,
                    wait_time=1,
                ):
                    message_id = int(message.id)
                    if not lower_id < message_id < resume_max_id:
                        raise HistoryPurgeError("Telegram 返回了清理范围外的消息，任务已中止。")
                    batch.append(message_id)
                    if len(batch) == HISTORY_PURGE_BATCH_SIZE:
                        (
                            batch_deleted,
                            batch_skipped,
                            batch_failed,
                        ) = await HistoryPurger._delete_batch(
                            client,
                            entity,
                            batch,
                            flood_wait_used=flood_wait_used,
                        )
                        deleted += batch_deleted
                        skipped += batch_skipped
                        failed += batch_failed
                        resume_max_id = min(batch)
                        batch = []
                if batch:
                    batch_deleted, batch_skipped, batch_failed = await HistoryPurger._delete_batch(
                        client,
                        entity,
                        batch,
                        flood_wait_used=flood_wait_used,
                    )
                    deleted += batch_deleted
                    skipped += batch_skipped
                    failed += batch_failed
                return deleted, skipped, failed
            except FloodWaitError as exc:
                await HistoryPurger._wait_for_flood(exc, flood_wait_used=flood_wait_used)
                # A partial in-memory page was not deleted, so retry the same
                # committed range boundary after the single permitted wait.
        return deleted, skipped, failed

    @staticmethod
    async def _delete_batch(
        client: TelegramClient,
        entity: Any,
        message_ids: list[int],
        *,
        flood_wait_used: list[bool],
    ) -> tuple[int, int, int]:
        try:
            await HistoryPurger._delete_with_flood_retry(
                client,
                entity,
                message_ids,
                flood_wait_used=flood_wait_used,
            )
        except RPCError:
            deleted = skipped = failed = 0
            for message_id in message_ids:
                try:
                    await HistoryPurger._delete_with_flood_retry(
                        client,
                        entity,
                        [message_id],
                        flood_wait_used=flood_wait_used,
                    )
                except RPCError:
                    skipped += 1
                except HistoryPurgeError:
                    raise
                except Exception:
                    failed += 1
                else:
                    deleted += 1
            return deleted, skipped, failed
        return len(message_ids), 0, 0

    @staticmethod
    async def _delete_with_flood_retry(
        client: TelegramClient,
        entity: Any,
        message_ids: list[int],
        *,
        flood_wait_used: list[bool],
    ) -> None:
        try:
            await client.delete_messages(entity, message_ids)
        except FloodWaitError as exc:
            await HistoryPurger._wait_for_flood(exc, flood_wait_used=flood_wait_used)
        else:
            return
        try:
            await client.delete_messages(entity, message_ids)
        except FloodWaitError as exc:
            raise HistoryPurgeError("Telegram 再次限流，清理已中止。") from exc

    @staticmethod
    async def _wait_for_flood(
        error: FloodWaitError,
        *,
        flood_wait_used: list[bool],
    ) -> None:
        if flood_wait_used[0]:
            raise HistoryPurgeError("Telegram 再次限流，清理已中止。") from error
        if error.seconds > HISTORY_PURGE_MAX_FLOOD_WAIT_SECONDS:
            raise HistoryPurgeError("Telegram 限流超过 300 秒，清理已中止。") from error
        flood_wait_used[0] = True
        await asyncio.sleep(error.seconds + 1)
