from pathlib import Path
from types import SimpleNamespace

import pytest

from nexa_guardian import history_purge as purge_module
from nexa_guardian.history_purge import (
    HistoryPurgeError,
    HistoryPurger,
    TelegramAdminSession,
    TelegramAdminSessionError,
)


async def test_history_purge_requires_configuration(tmp_path: Path) -> None:
    purger = HistoryPurger(
        api_id=None,
        api_hash="",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
    )
    with pytest.raises(HistoryPurgeError, match="尚未配置"):
        await purger.purge(-100)


async def test_shared_admin_session_rejects_concurrent_mtproto_work(tmp_path: Path) -> None:
    session = TelegramAdminSession(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "admin",
        authorized_user_ids=frozenset({7}),
    )
    await session._lock.acquire()
    try:
        with pytest.raises(TelegramAdminSessionError, match="已有 MTProto"):
            async with session.connect(-100):
                raise AssertionError("unreachable")
    finally:
        session._lock.release()


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
            assert flood_sleep_threshold == 30
            self.deleted: list[tuple[int, ...]] = []
            self.scans = 0
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

        def iter_messages(self, entity: int):  # type: ignore[no-untyped-def]
            self.scans += 1
            ids = (3, 2, 1) if self.scans == 1 else ()

            async def iterator():  # type: ignore[no-untyped-def]
                for message_id in ids:
                    yield SimpleNamespace(id=message_id)

            return iterator()

        async def delete_messages(self, entity: int, message_ids: list[int]) -> None:
            self.deleted.append(tuple(message_ids))

        def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setattr(purge_module, "TelegramClient", FakeClient)
    purger = HistoryPurger(
        api_id=12345,
        api_hash="test-hash",
        session_path=tmp_path / "session" / "admin",
        authorized_user_ids=frozenset({7}),
    )

    result = await purger.purge(-100)

    assert (result.deleted, result.skipped, result.failed) == (3, 0, 0)
    assert FakeClient.instances[0].deleted == [(3, 2, 1)]
    assert FakeClient.instances[0].disconnected
