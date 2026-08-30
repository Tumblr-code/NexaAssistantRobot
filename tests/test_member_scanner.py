from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import types
from telethon.errors import FloodWaitError, RPCError

from nexa_guardian.member_cleanup import (
    REASON_DELETED,
    REASON_LAST_MONTH,
    REASON_OFFLINE,
    REASON_SILENT,
    MemberScanError,
    MemberScanner,
)

UTC = timezone.utc


class FakeSession:
    def __init__(self, client: Any) -> None:
        self.client = client

    @asynccontextmanager
    async def connect(self, chat_id: int):  # type: ignore[no-untyped-def]
        assert chat_id == -100
        yield self.client, "group"


class FakeClient:
    def __init__(self, users: list[Any], participants: list[Any], messages: list[Any]) -> None:
        self.users = users
        self.participants = participants
        self.messages = messages
        self.page_calls = 0
        self.history_calls = 0

    async def get_input_entity(self, entity: str) -> str:
        assert entity == "group"
        return entity

    async def __call__(self, request: Any) -> Any:
        self.page_calls += 1
        if request.offset:
            return SimpleNamespace(users=[], participants=[], count=len(self.participants))
        return SimpleNamespace(
            users=self.users,
            participants=self.participants,
            count=len(self.participants),
        )

    def iter_messages(self, entity: str):  # type: ignore[no-untyped-def]
        assert entity == "group"
        self.history_calls += 1

        async def iterator():  # type: ignore[no-untyped-def]
            for message in self.messages:
                yield message

        return iterator()


def user(
    user_id: int,
    *,
    status: Any = None,
    deleted: bool = False,
    bot: bool = False,
) -> types.User:
    return types.User(id=user_id, status=status, deleted=deleted, bot=bot)


async def test_scanner_classifies_candidates_deduplicates_and_scans_history_once() -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=30)
    joined = now - timedelta(days=60)
    users = [
        user(1, deleted=True),
        user(2, status=types.UserStatusOffline(cutoff - timedelta(days=20))),
        user(3, status=types.UserStatusLastMonth()),
        user(4, status=types.UserStatusRecently()),
        user(5, status=types.UserStatusRecently()),
        user(6, deleted=True, bot=True),
    ]
    participants = [types.ChannelParticipant(item.id, joined) for item in users]
    messages = [
        SimpleNamespace(date=now - timedelta(days=1), sender_id=4),
        SimpleNamespace(date=cutoff - timedelta(seconds=1), sender_id=99),
    ]
    client = FakeClient(users, participants, messages)
    scanner = MemberScanner(FakeSession(client), authorized_user_ids=frozenset())  # type: ignore[arg-type]

    result = await scanner.scan(-100, cutoff=cutoff)

    by_id = {candidate.user_id: candidate.reasons for candidate in result.candidates}
    assert by_id[1] == (REASON_DELETED, REASON_SILENT)
    assert by_id[2] == (REASON_OFFLINE, REASON_SILENT)
    assert by_id[3] == (REASON_LAST_MONTH, REASON_SILENT)
    assert by_id[5] == (REASON_SILENT,)
    assert 4 not in by_id
    assert 6 not in by_id
    assert client.history_calls == 1


async def test_scanner_excludes_recent_joiner_unknown_status_and_authorized_admin() -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=30)
    users = [user(7), user(8), user(9), user(10)]
    participants = [
        types.ChannelParticipant(7, now - timedelta(days=90)),
        types.ChannelParticipant(8, now - timedelta(days=5)),
        types.ChannelParticipant(9, None),
        types.ChannelParticipant(10, now - timedelta(days=90)),
    ]
    scanner = MemberScanner(
        FakeSession(FakeClient(users, participants, [])),  # type: ignore[arg-type]
        authorized_user_ids=frozenset({7}),
    )

    result = await scanner.scan(-100, cutoff=cutoff)

    assert result.candidates == ()


async def test_scanner_paginates_members_in_batches_of_200() -> None:
    class PagedClient(FakeClient):
        async def __call__(self, request: Any) -> Any:
            self.page_calls += 1
            start = request.offset
            end = start + request.limit
            return SimpleNamespace(
                users=self.users[start:end],
                participants=self.participants[start:end],
                count=len(self.participants),
            )

    now = datetime.now(UTC)
    users = [user(user_id, deleted=True) for user_id in range(1, 202)]
    participants = [types.ChannelParticipant(item.id, now - timedelta(days=90)) for item in users]
    client = PagedClient(
        users,
        participants,
        [SimpleNamespace(date=now - timedelta(days=31), sender_id=999)],
    )
    scanner = MemberScanner(FakeSession(client), authorized_user_ids=frozenset())  # type: ignore[arg-type]

    result = await scanner.scan(-100, cutoff=now - timedelta(days=30))

    assert client.page_calls == 2
    assert result.participants_scanned == 201
    assert len(result.candidates) == 201


async def test_history_failure_disables_silent_only_candidates() -> None:
    class BrokenHistoryClient(FakeClient):
        def iter_messages(self, entity: str):  # type: ignore[no-untyped-def]
            async def iterator():  # type: ignore[no-untyped-def]
                raise RPCError(None, "history failed")
                yield

            return iterator()

    now = datetime.now(UTC)
    users = [user(10), user(11, deleted=True)]
    participants = [types.ChannelParticipant(item.id, now - timedelta(days=90)) for item in users]
    scanner = MemberScanner(
        FakeSession(BrokenHistoryClient(users, participants, [])),  # type: ignore[arg-type]
        authorized_user_ids=frozenset(),
    )

    result = await scanner.scan(-100, cutoff=now - timedelta(days=30))

    assert not result.history_complete
    assert [(item.user_id, item.reasons) for item in result.candidates] == [(11, (REASON_DELETED,))]


async def test_long_flood_wait_aborts_member_scan_without_recursive_retry() -> None:
    class FloodedClient:
        async def get_input_entity(self, entity: str) -> str:
            return entity

        async def __call__(self, request: Any) -> Any:
            raise FloodWaitError(request, capture=31)

    scanner = MemberScanner(FakeSession(FloodedClient()), authorized_user_ids=frozenset())  # type: ignore[arg-type]

    with pytest.raises(MemberScanError, match="超过 30 秒"):
        await scanner.scan(-100, cutoff=datetime.now(UTC) - timedelta(days=30))
