from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import FloodWaitError, RPCError

from nexa_guardian import history_purge as purge_module
from nexa_guardian.history_purge import (
    HistoryPurgeError,
    HistoryPurger,
    TelegramAdminSession,
    TelegramAdminSessionError,
)


def write_session(path: Path, *, mode: int = 0o600) -> Path:
    session_file = Path(f"{path}.session")
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_bytes(b"test-session")
    session_file.chmod(mode)
    return session_file


def make_session_symlink(path: Path, target: Path) -> None:
    Path(f"{path}.session").symlink_to(target)


async def test_history_purge_requires_configuration(tmp_path: Path) -> None:
    purger = HistoryPurger(
        api_id=None,
        api_hash="",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )
    with pytest.raises(HistoryPurgeError, match="尚未配置"):
        await purger.purge(-100)


async def test_history_purge_wraps_unexpected_iteration_failure(tmp_path: Path) -> None:
    class FailingClient:
        def iter_messages(
            self,
            _entity: object,
            **_kwargs: object,
        ):  # type: ignore[no-untyped-def]
            async def iterator():  # type: ignore[no-untyped-def]
                raise RuntimeError("private transport detail")
                yield SimpleNamespace(id=1)

            return iterator()

    class FakeSession:
        configured = True

        @asynccontextmanager
        async def connect(self, _chat_id: int):  # type: ignore[no-untyped-def]
            yield FailingClient(), object()

    purger = HistoryPurger(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
        session=FakeSession(),  # type: ignore[arg-type]
    )

    with pytest.raises(HistoryPurgeError, match="Telegram 群历史清理失败。") as captured:
        await purger.purge(-100)

    assert "private transport detail" not in str(captured.value)


async def test_shared_admin_session_rejects_concurrent_mtproto_work(tmp_path: Path) -> None:
    session = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )
    await session._lock.acquire()
    try:
        with pytest.raises(TelegramAdminSessionError, match="已有 MTProto"):
            async with session.connect(-100):
                raise AssertionError("unreachable")
    finally:
        session._lock.release()


async def test_admin_session_probe_distinguishes_unconfigured_and_busy(tmp_path: Path) -> None:
    unconfigured = TelegramAdminSession(
        api_id=None,
        api_hash="",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )
    health = await unconfigured.probe(-100)
    assert not health.configured
    assert not health.verified
    assert health.detail == "未配置 Telegram API 凭据"

    configured = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )
    await configured._lock.acquire()
    try:
        health = await configured.probe(-100)
    finally:
        configured._lock.release()
    assert health.configured
    assert not health.verified
    assert health.detail == "任务运行中"


async def test_history_purge_scans_three_times_and_verifies_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        instances: list["FakeClient"] = []

        def __init__(
            self,
            session: str,
            api_id: int,
            api_hash: str,
            *,
            flood_sleep_threshold: int,
        ) -> None:
            assert flood_sleep_threshold == 0
            self.deleted: list[tuple[int, ...]] = []
            self.scans = 0
            self.remaining = {1, 2, 3}
            self.disconnected = False
            self.instances.append(self)

        async def connect(self) -> None:
            return None

        async def is_user_authorized(self) -> bool:
            return True

        async def get_me(self) -> SimpleNamespace:
            return SimpleNamespace(id=7)

        async def get_entity(self, chat_id: int) -> int:
            return chat_id

        def iter_messages(
            self,
            entity: int,
            **kwargs: int,
        ):  # type: ignore[no-untyped-def]
            self.scans += 1
            if kwargs.get("limit") == 1:
                ids = tuple(sorted(self.remaining, reverse=True)[:1])
            else:
                min_id = kwargs.get("min_id", 0)
                max_id = kwargs.get("max_id", 2**63)
                ids = tuple(
                    message_id
                    for message_id in sorted(self.remaining, reverse=True)
                    if min_id < message_id < max_id
                )

            async def iterator():  # type: ignore[no-untyped-def]
                for message_id in ids:
                    yield SimpleNamespace(id=message_id)

            return iterator()

        async def delete_messages(self, entity: int, message_ids: list[int]) -> None:
            self.deleted.append(tuple(message_ids))
            self.remaining.difference_update(message_ids)

        def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setattr(purge_module, "TelegramClient", FakeClient)
    session_path = tmp_path / "session" / "admin"
    write_session(session_path)
    purger = HistoryPurger(
        api_id=12345,
        api_hash="test-hash",
        session_path=session_path,
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )

    result = await purger.purge(-100)

    assert (result.deleted, result.skipped, result.failed) == (3, 0, 0)
    assert FakeClient.instances[0].deleted == [(3, 2, 1)]
    assert FakeClient.instances[0].disconnected


async def test_shared_admin_session_rejects_chat_outside_allowlist(tmp_path: Path) -> None:
    session = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )

    with pytest.raises(TelegramAdminSessionError, match="非目标群"):
        async with session.connect(-200):
            raise AssertionError("unreachable")


async def test_history_purge_streams_large_history_in_bounded_batches(tmp_path: Path) -> None:
    class StreamingClient:
        def __init__(self) -> None:
            self.remaining = set(range(1, 251))
            self.events: list[tuple[str, int]] = []
            self.deleted_batches: list[tuple[int, ...]] = []

        def iter_messages(
            self,
            _entity: object,
            **kwargs: int,
        ):  # type: ignore[no-untyped-def]
            limit = kwargs.get("limit")
            min_id = kwargs.get("min_id", 0)
            max_id = kwargs.get("max_id", 2**63)
            candidate_count = sum(min_id < message_id < max_id for message_id in self.remaining)
            if limit is not None:
                candidate_count = min(candidate_count, limit)
            self.events.append(("fetch", candidate_count))

            async def iterator():  # type: ignore[no-untyped-def]
                yielded = 0
                upper = min(max_id - 1, max(self.remaining, default=0))
                for message_id in range(upper, min_id, -1):
                    if message_id not in self.remaining:
                        continue
                    if limit is not None and yielded >= limit:
                        break
                    if limit is None and yielded == 100:
                        assert self.deleted_batches
                    yielded += 1
                    yield SimpleNamespace(id=message_id)

            return iterator()

        async def delete_messages(
            self,
            _entity: object,
            message_ids: list[int],
        ) -> None:
            self.events.append(("delete", len(message_ids)))
            self.deleted_batches.append(tuple(message_ids))
            self.remaining.difference_update(message_ids)

    class FakeSession:
        configured = True

        def __init__(self, client: StreamingClient) -> None:
            self.client = client

        @asynccontextmanager
        async def connect(self, _chat_id: int):  # type: ignore[no-untyped-def]
            yield self.client, object()

    client = StreamingClient()
    purger = HistoryPurger(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
        session=FakeSession(client),  # type: ignore[arg-type]
    )

    result = await purger.purge(-100)

    assert result.deleted == 250
    assert result.skipped == result.failed == 0
    assert client.remaining == set()
    assert [len(batch) for batch in client.deleted_batches] == [100, 100, 50]
    assert all(len(batch) <= 100 for batch in client.deleted_batches)
    assert client.events[0] == ("fetch", 1)
    assert client.events[1:4] == [("fetch", 250), ("delete", 100), ("delete", 100)]


async def test_history_purge_resumes_after_partial_page_flood_without_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PartialFloodClient:
        def __init__(self) -> None:
            self.remaining = set(range(1, 251))
            self.range_calls = 0
            self.deleted_batches: list[tuple[int, ...]] = []

        def iter_messages(
            self,
            _entity: object,
            **kwargs: int,
        ):  # type: ignore[no-untyped-def]
            limit = kwargs.get("limit")
            min_id = kwargs.get("min_id", 0)
            max_id = kwargs.get("max_id", 2**63)
            if limit != 1:
                self.range_calls += 1
            range_call = self.range_calls

            async def iterator():  # type: ignore[no-untyped-def]
                yielded = 0
                upper = min(max_id - 1, max(self.remaining, default=0))
                for message_id in range(upper, min_id, -1):
                    if message_id not in self.remaining:
                        continue
                    if limit is not None and yielded >= limit:
                        break
                    yielded += 1
                    yield SimpleNamespace(id=message_id)
                    if limit is None and range_call == 1 and yielded == 120:
                        raise FloodWaitError(request=None, capture=1)

            return iterator()

        async def delete_messages(
            self,
            _entity: object,
            message_ids: list[int],
        ) -> None:
            self.deleted_batches.append(tuple(message_ids))
            self.remaining.difference_update(message_ids)

    class FakeSession:
        configured = True

        def __init__(self, client: PartialFloodClient) -> None:
            self.client = client

        @asynccontextmanager
        async def connect(self, _chat_id: int):  # type: ignore[no-untyped-def]
            yield self.client, object()

    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(purge_module.asyncio, "sleep", fake_sleep)
    client = PartialFloodClient()
    purger = HistoryPurger(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
        session=FakeSession(client),  # type: ignore[arg-type]
    )

    result = await purger.purge(-100)

    deleted_ids = [message_id for batch in client.deleted_batches for message_id in batch]
    assert result.deleted == 250
    assert result.skipped == result.failed == 0
    assert client.remaining == set()
    assert len(deleted_ids) == len(set(deleted_ids)) == 250
    assert sleeps == [2]


@pytest.mark.parametrize(
    ("single_error", "expected"),
    (
        (RPCError(None, "permanent"), (0, 1, 0)),
        (RuntimeError("unknown transport outcome"), (0, 0, 1)),
    ),
)
async def test_history_purge_counts_each_failed_message_once_across_tail_scans(
    tmp_path: Path,
    single_error: Exception,
    expected: tuple[int, int, int],
) -> None:
    class OneFailureClient:
        def iter_messages(
            self,
            _entity: object,
            **kwargs: int,
        ):  # type: ignore[no-untyped-def]
            async def iterator():  # type: ignore[no-untyped-def]
                if kwargs.get("limit") == 1 or kwargs.get("min_id", 0) < 1:
                    yield SimpleNamespace(id=1)

            return iterator()

        async def delete_messages(
            self,
            _entity: object,
            message_ids: list[int],
        ) -> None:
            if len(message_ids) > 1:
                raise AssertionError("single-message fixture received a batch")
            if not hasattr(self, "batch_failed"):
                self.batch_failed = True
                raise RPCError(None, "batch failure")
            raise single_error

    class FakeSession:
        configured = True

        def __init__(self, client: OneFailureClient) -> None:
            self.client = client

        @asynccontextmanager
        async def connect(self, _chat_id: int):  # type: ignore[no-untyped-def]
            yield self.client, object()

    client = OneFailureClient()
    purger = HistoryPurger(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
        session=FakeSession(client),  # type: ignore[arg-type]
    )

    result = await purger.purge(-100)

    assert (result.deleted, result.skipped, result.failed) == expected


@pytest.mark.parametrize("mode", [0o601, 0o640, 0o700])
async def test_shared_admin_session_rejects_permissions_wider_than_0600(
    tmp_path: Path, mode: int
) -> None:
    session_path = tmp_path / "admin"
    write_session(session_path, mode=mode)
    session = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=session_path,
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )

    with pytest.raises(TelegramAdminSessionError, match="权限不得宽于 0600"):
        async with session.connect(-100):
            raise AssertionError("unreachable")


async def test_shared_admin_session_rejects_symlink(tmp_path: Path) -> None:
    real_session = tmp_path / "real.session"
    real_session.write_bytes(b"test-session")
    real_session.chmod(0o600)
    session_path = tmp_path / "admin"
    make_session_symlink(session_path, real_session)
    session = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=session_path,
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )

    with pytest.raises(TelegramAdminSessionError, match="非符号链接"):
        async with session.connect(-100):
            raise AssertionError("unreachable")


async def test_shared_admin_session_setup_has_total_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowClient:
        instances: list["SlowClient"] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.disconnected = False
            self.instances.append(self)

        async def connect(self) -> None:
            await purge_module.asyncio.sleep(60)

        def disconnect(self) -> None:
            self.disconnected = True

    session_path = tmp_path / "admin"
    write_session(session_path)
    monkeypatch.setattr(purge_module, "TelegramClient", SlowClient)
    monkeypatch.setattr(purge_module, "SESSION_SETUP_TIMEOUT_SECONDS", 0.01)
    session = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=session_path,
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )

    with pytest.raises(TelegramAdminSessionError, match="超过 15 秒"):
        async with session.connect(-100):
            raise AssertionError("unreachable")

    assert SlowClient.instances[0].disconnected


async def test_admin_session_probe_verifies_identity_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProbeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.disconnected = False

        async def connect(self) -> None:
            return None

        async def is_user_authorized(self) -> bool:
            return True

        async def get_me(self) -> SimpleNamespace:
            return SimpleNamespace(id=7)

        async def get_entity(self, chat_id: int) -> SimpleNamespace:
            assert chat_id == -100
            return SimpleNamespace(id=chat_id)

        def disconnect(self) -> None:
            self.disconnected = True

    session_path = tmp_path / "admin"
    write_session(session_path)
    monkeypatch.setattr(purge_module, "TelegramClient", ProbeClient)
    session = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=session_path,
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )

    health = await session.probe(-100)

    assert health.configured
    assert health.verified
    assert health.detail == "身份与目标群已验证"


async def test_admin_session_probe_returns_safe_setup_error(tmp_path: Path) -> None:
    session = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "private" / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
    )

    health = await session.probe(-100)

    assert health.configured
    assert not health.verified
    assert health.detail == "管理员 Telegram 用户会话文件不存在。"
    assert str(tmp_path) not in health.detail


async def test_history_purge_uses_only_one_flood_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FloodedClient:
        calls = 0

        async def delete_messages(self, entity: int, message_ids: list[int]) -> None:
            self.calls += 1
            raise FloodWaitError(request=None, capture=1)

    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(purge_module.asyncio, "sleep", fake_sleep)
    client: Any = FloodedClient()

    with pytest.raises(HistoryPurgeError, match="再次限流"):
        await HistoryPurger._delete_with_flood_retry(client, 1, [1], flood_wait_used=[False])

    assert client.calls == 2
    assert sleeps == [2]


async def test_fallback_delete_does_not_swallow_second_flood_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FloodedFallbackClient:
        calls = 0

        def iter_messages(
            self,
            _entity: object,
            **kwargs: int,
        ):  # type: ignore[no-untyped-def]
            async def iterator():  # type: ignore[no-untyped-def]
                if kwargs.get("limit") == 1 or kwargs.get("min_id", 0) < 1:
                    yield SimpleNamespace(id=1)

            return iterator()

        async def delete_messages(self, _entity: object, _message_ids: list[int]) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RPCError(None, "batch delete failed")
            raise FloodWaitError(request=None, capture=1)

    class FakeSession:
        configured = True

        def __init__(self, client: FloodedFallbackClient) -> None:
            self.client = client

        @asynccontextmanager
        async def connect(self, _chat_id: int):  # type: ignore[no-untyped-def]
            yield self.client, object()

    sleeps: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(purge_module.asyncio, "sleep", fake_sleep)
    client = FloodedFallbackClient()
    purger = HistoryPurger(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
        allowed_chat_ids=frozenset({-100}),
        session=FakeSession(client),  # type: ignore[arg-type]
    )

    with pytest.raises(HistoryPurgeError, match="再次限流"):
        await purger.purge(-100)

    assert client.calls == 3
    assert sleeps == [2]
