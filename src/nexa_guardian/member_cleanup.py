from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telethon import functions, types
from telethon.errors import FloodWaitError, RPCError

from nexa_guardian.history_purge import TelegramAdminSession, TelegramAdminSessionError

PARTICIPANT_PAGE_SIZE = 200
SCAN_FLOOD_WAIT_LIMIT_SECONDS = 30

REASON_DELETED = "deleted"
REASON_OFFLINE = "offline"
REASON_LAST_MONTH = "last_month"
REASON_SILENT = "silent"


class MemberScanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MemberState:
    user_id: int
    deleted: bool
    bot: bool
    administrator: bool
    joined_at: datetime | None
    status_reason: str | None
    last_online_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemberCandidate:
    user_id: int
    reasons: tuple[str, ...]
    last_online_at: datetime | None
    joined_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemberScanResult:
    candidates: tuple[MemberCandidate, ...]
    history_complete: bool
    participants_scanned: int


class MemberScanner:
    """Read-only member scanner backed by the authorized MTProto user session."""

    def __init__(
        self,
        session: TelegramAdminSession,
        *,
        authorized_user_ids: frozenset[int],
    ) -> None:
        self.session = session
        self.authorized_user_ids = authorized_user_ids

    async def scan(self, chat_id: int, *, cutoff: datetime) -> MemberScanResult:
        try:
            async with self.session.connect(chat_id) as (client, entity):
                states = await self._participant_states(client, entity, cutoff=cutoff)
                active_ids, history_complete = await self._active_member_ids(
                    client,
                    entity,
                    cutoff=cutoff,
                )
        except TelegramAdminSessionError as exc:
            raise MemberScanError(str(exc)) from exc
        except RPCError as exc:
            raise MemberScanError("Telegram 成员列表读取失败。") from exc

        candidates: list[MemberCandidate] = []
        for state in states.values():
            reasons = self._candidate_reasons(
                state,
                cutoff=cutoff,
                active_ids=active_ids,
                history_complete=history_complete,
            )
            if reasons:
                candidates.append(
                    MemberCandidate(
                        user_id=state.user_id,
                        reasons=reasons,
                        last_online_at=state.last_online_at,
                        joined_at=state.joined_at,
                    )
                )
        candidates.sort(key=self._candidate_sort_key)
        return MemberScanResult(
            candidates=tuple(candidates),
            history_complete=history_complete,
            participants_scanned=len(states),
        )

    async def current_members(
        self,
        chat_id: int,
        *,
        cutoff: datetime,
        activity_since: datetime,
        user_ids: frozenset[int],
    ) -> tuple[dict[int, MemberState], frozenset[int]]:
        try:
            async with self.session.connect(chat_id) as (client, entity):
                states = await self._participant_states(client, entity, cutoff=cutoff)
                active_ids, history_complete = await self._active_member_ids(
                    client,
                    entity,
                    cutoff=activity_since,
                )
        except TelegramAdminSessionError as exc:
            raise MemberScanError(str(exc)) from exc
        except RPCError as exc:
            raise MemberScanError("Telegram 成员复核失败。") from exc
        if not history_complete:
            raise MemberScanError("无法完整核验预览后的群活动。")
        return (
            {user_id: states[user_id] for user_id in user_ids if user_id in states},
            frozenset(user_ids.intersection(active_ids)),
        )

    async def _participant_states(
        self,
        client: Any,
        entity: Any,
        *,
        cutoff: datetime,
    ) -> dict[int, MemberState]:
        input_channel = await client.get_input_entity(entity)
        offset = 0
        states: dict[int, MemberState] = {}
        while True:
            request = functions.channels.GetParticipantsRequest(
                channel=input_channel,
                filter=types.ChannelParticipantsSearch(""),
                offset=offset,
                limit=PARTICIPANT_PAGE_SIZE,
                hash=0,
            )
            result = await self._call_with_flood_retry(client, request)
            participants = {
                int(participant.user_id): participant
                for participant in result.participants
                if getattr(participant, "user_id", None) is not None
            }
            for user in result.users:
                user_id = int(user.id)
                participant = participants.get(user_id)
                if user_id in self.authorized_user_ids or participant is None:
                    continue
                administrator = isinstance(
                    participant,
                    (types.ChannelParticipantAdmin, types.ChannelParticipantCreator),
                )
                joined_at = self._aware_datetime(getattr(participant, "date", None))
                status_reason, last_online_at = self._status_evidence(user, cutoff=cutoff)
                states[user_id] = MemberState(
                    user_id=user_id,
                    deleted=bool(getattr(user, "deleted", False)),
                    bot=bool(getattr(user, "bot", False)),
                    administrator=administrator,
                    joined_at=joined_at,
                    status_reason=status_reason,
                    last_online_at=last_online_at,
                )
            offset += len(result.participants)
            if not result.participants or offset >= int(result.count):
                break
        return states

    @staticmethod
    async def _call_with_flood_retry(client: Any, request: Any) -> Any:
        try:
            return await client(request)
        except FloodWaitError as exc:
            if exc.seconds > SCAN_FLOOD_WAIT_LIMIT_SECONDS:
                raise MemberScanError("Telegram 限流超过 30 秒，成员扫描已中止。") from exc
            await asyncio.sleep(exc.seconds + 1)
            return await client(request)

    @staticmethod
    async def _active_member_ids(
        client: Any,
        entity: Any,
        *,
        cutoff: datetime,
    ) -> tuple[frozenset[int], bool]:
        active: set[int] = set()
        boundary_seen = False
        try:
            async for message in client.iter_messages(entity):
                sent_at = MemberScanner._aware_datetime(getattr(message, "date", None))
                if sent_at is None:
                    continue
                if sent_at < cutoff:
                    boundary_seen = True
                    break
                sender_id = getattr(message, "sender_id", None)
                if sender_id is not None:
                    active.add(int(sender_id))
        except (FloodWaitError, RPCError):
            return frozenset(), False
        return frozenset(active), boundary_seen

    @staticmethod
    def _status_evidence(user: Any, *, cutoff: datetime) -> tuple[str | None, datetime | None]:
        status = getattr(user, "status", None)
        if isinstance(status, types.UserStatusOffline):
            last_online_at = MemberScanner._aware_datetime(status.was_online)
            if last_online_at is not None and last_online_at < cutoff:
                return REASON_OFFLINE, last_online_at
            return None, last_online_at
        if isinstance(status, types.UserStatusLastMonth):
            return REASON_LAST_MONTH, None
        return None, None

    @staticmethod
    def _candidate_reasons(
        state: MemberState,
        *,
        cutoff: datetime,
        active_ids: frozenset[int],
        history_complete: bool,
    ) -> tuple[str, ...]:
        if state.bot or state.administrator:
            return ()
        reasons: list[str] = []
        if state.deleted:
            reasons.append(REASON_DELETED)
        if state.status_reason is not None:
            reasons.append(state.status_reason)
        if (
            history_complete
            and state.joined_at is not None
            and state.joined_at <= cutoff
            and state.user_id not in active_ids
        ):
            reasons.append(REASON_SILENT)
        return tuple(reasons)

    @staticmethod
    def _candidate_sort_key(candidate: MemberCandidate) -> tuple[int, float, int]:
        priorities = {
            REASON_DELETED: 0,
            REASON_OFFLINE: 1,
            REASON_LAST_MONTH: 2,
            REASON_SILENT: 3,
        }
        primary = min(priorities[reason] for reason in candidate.reasons)
        last_online = (
            candidate.last_online_at.timestamp()
            if candidate.last_online_at is not None
            else float("inf")
        )
        return primary, last_online, candidate.user_id

    @staticmethod
    def _aware_datetime(value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
