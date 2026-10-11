from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count

DEFAULT_MEMBER_ACTIVITY_TTL_SECONDS = 600.0


class MemberActivityTrackerError(RuntimeError):
    """Base error for invalid member-cleanup activity plan operations."""


class MemberActivityPlanNotFound(MemberActivityTrackerError):
    """The referenced plan was replaced, expired, consumed, or finished."""


class MemberActivityPlanBusy(MemberActivityTrackerError):
    """A cleanup plan for the same administrator and group is executing."""


@dataclass(frozen=True, slots=True)
class MemberActivitySession:
    """Opaque handle returned before a member scan starts."""

    session_id: int
    actor_id: int
    chat_id: int
    cutoff: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemberActivityPlan:
    """Public metadata for a confirmation-bound activity session."""

    session: MemberActivitySession
    confirmation_code: str
    candidate_ids: frozenset[int]
    expires_at: float
    executing: bool

    @property
    def actor_id(self) -> int:
        return self.session.actor_id

    @property
    def chat_id(self) -> int:
        return self.session.chat_id

    @property
    def cutoff(self) -> datetime:
        return self.session.cutoff

    @property
    def created_at(self) -> datetime:
        return self.session.created_at


@dataclass(slots=True)
class _PlanState:
    session: MemberActivitySession
    expires_at: float
    ttl_seconds: float
    confirmation_code: str | None = None
    candidate_ids: frozenset[int] | None = None
    active_at: dict[int, datetime] | None = None
    executing: bool = False

    def __post_init__(self) -> None:
        if self.active_at is None:
            self.active_at = {}


PlanReference = MemberActivitySession | MemberActivityPlan


class MemberActivityTracker:
    """Track activity only while a member-cleanup plan exists.

    The tracker is intentionally synchronous and memory-only. Aiogram update
    handlers can therefore call :meth:`note` on the hot path without awaiting a
    database write. It stores only Telegram identifiers and UTC timestamps.
    Restarting the process naturally discards every pending plan.
    """

    def __init__(
        self,
        *,
        default_ttl_seconds: float = DEFAULT_MEMBER_ACTIVITY_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._validate_ttl(default_ttl_seconds)
        self.default_ttl_seconds = float(default_ttl_seconds)
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._ids = count(1)
        self._states: dict[int, _PlanState] = {}
        self._by_owner_chat: dict[tuple[int, int], int] = {}
        self._by_chat: dict[int, set[int]] = {}
        self._by_confirmation: dict[tuple[int, str], int] = {}

    def begin(
        self,
        *,
        actor_id: int,
        chat_id: int,
        cutoff: datetime,
        created_at: datetime,
        ttl_seconds: float | None = None,
    ) -> MemberActivitySession:
        """Start tracking before the initial MTProto scan.

        A newer preview may replace an already-bound pending plan for the same
        administrator/group pair. A scan or execution already in progress must
        finish first.
        """

        cutoff_utc = self._as_utc(cutoff, field="cutoff")
        created_at_utc = self._as_utc(created_at, field="created_at")
        if cutoff_utc > created_at_utc:
            raise ValueError("cutoff must not be later than created_at")
        ttl = self.default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        self._validate_ttl(ttl)
        self.expire()

        owner_key = (int(actor_id), int(chat_id))
        previous_id = self._by_owner_chat.get(owner_key)
        if previous_id is not None:
            previous = self._states.get(previous_id)
            if previous is not None and (previous.executing or previous.confirmation_code is None):
                raise MemberActivityPlanBusy(
                    "a member-cleanup scan or execution is already in progress"
                )
            self._discard(previous_id)

        session = MemberActivitySession(
            session_id=next(self._ids),
            actor_id=int(actor_id),
            chat_id=int(chat_id),
            cutoff=cutoff_utc,
            created_at=created_at_utc,
        )
        state = _PlanState(
            session=session,
            expires_at=self._clock() + ttl,
            ttl_seconds=ttl,
        )
        self._states[session.session_id] = state
        self._by_owner_chat[owner_key] = session.session_id
        self._by_chat.setdefault(session.chat_id, set()).add(session.session_id)
        return session

    def bind(
        self,
        session: MemberActivitySession,
        *,
        confirmation_code: str,
        candidate_ids: Iterable[int],
    ) -> MemberActivityPlan:
        """Bind candidates and a confirmation code after scanning.

        Activity captured while the scan was running is retained for selected
        candidates. After binding, messages from non-candidates are ignored.
        """

        state = self._live_state(session)
        if state.executing:
            raise MemberActivityPlanBusy("the member-cleanup plan is already executing")
        code = self._normalize_code(confirmation_code)
        confirmation_key = (session.actor_id, code)
        bound_id = self._by_confirmation.get(confirmation_key)
        if bound_id is not None and bound_id != session.session_id:
            raise ValueError("confirmation code is already in use for this administrator")

        if state.confirmation_code is not None:
            self._by_confirmation.pop((session.actor_id, state.confirmation_code), None)
        selected = frozenset(int(user_id) for user_id in candidate_ids)
        state.confirmation_code = code
        state.candidate_ids = selected
        state.expires_at = self._clock() + state.ttl_seconds
        assert state.active_at is not None
        state.active_at = {
            user_id: observed_at
            for user_id, observed_at in state.active_at.items()
            if user_id in selected
        }
        self._by_confirmation[confirmation_key] = session.session_id
        return self._snapshot(state)

    def get(self, *, actor_id: int, confirmation_code: str) -> MemberActivityPlan | None:
        """Return a live confirmation plan without consuming its code."""

        self.expire()
        code = self._normalize_code(confirmation_code)
        session_id = self._by_confirmation.get((int(actor_id), code))
        if session_id is None:
            return None
        state = self._states.get(session_id)
        if state is None or state.executing:
            return None
        return self._snapshot(state)

    def start_execution(
        self,
        *,
        actor_id: int,
        confirmation_code: str,
    ) -> MemberActivityPlan | None:
        """Consume a valid confirmation code and keep tracking until finish()."""

        self.expire()
        code = self._normalize_code(confirmation_code)
        key = (int(actor_id), code)
        session_id = self._by_confirmation.pop(key, None)
        if session_id is None:
            return None
        state = self._states.get(session_id)
        if state is None or state.executing or state.confirmation_code != code:
            return None
        state.executing = True
        return self._snapshot(state)

    def note(
        self,
        chat_id: int,
        user_id: int,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        """Record one member activity event for matching live plans.

        Returns whether at least one plan retained the event. The operation does
        no I/O and normally touches only one in-memory plan.
        """

        target_chat_id = int(chat_id)
        session_ids = self._by_chat.get(target_chat_id)
        if not session_ids:
            return False
        observed = self._as_utc(
            self._wall_clock() if observed_at is None else observed_at,
            field="observed_at",
        )

        now = self._clock()
        recorded = False
        for session_id in tuple(session_ids):
            state = self._states.get(session_id)
            if state is None:
                session_ids.discard(session_id)
                continue
            if not state.executing and state.expires_at <= now:
                self._discard(session_id)
                continue
            if observed < state.session.created_at:
                continue
            target_id = int(user_id)
            if state.candidate_ids is not None and target_id not in state.candidate_ids:
                continue
            assert state.active_at is not None
            previous = state.active_at.get(target_id)
            if previous is None or observed > previous:
                state.active_at[target_id] = observed
            recorded = True

        if not session_ids:
            self._by_chat.pop(int(chat_id), None)
        return recorded

    def active_ids(self, plan: PlanReference) -> frozenset[int]:
        """Return IDs active since this plan began, limited to bound candidates."""

        state = self._live_state(plan)
        assert state.active_at is not None
        return frozenset(state.active_at)

    def finish(self, plan: PlanReference) -> bool:
        """Remove a completed, cancelled, or failed plan."""

        session_id = self._session_id(plan)
        state = self._states.get(session_id)
        if state is None or not self._same_session(state, plan):
            return False
        self._discard(session_id)
        return True

    def expire(self) -> int:
        """Discard expired confirmation plans and return the number removed.

        A plan whose confirmation code was already consumed remains live until
        its execution calls :meth:`finish`, even if the original deadline passes.
        """

        now = self._clock()
        expired = [
            session_id
            for session_id, state in self._states.items()
            if not state.executing and state.expires_at <= now
        ]
        for session_id in expired:
            self._discard(session_id)
        return len(expired)

    def _live_state(self, plan: PlanReference) -> _PlanState:
        self.expire()
        session_id = self._session_id(plan)
        state = self._states.get(session_id)
        if state is None or not self._same_session(state, plan):
            raise MemberActivityPlanNotFound(
                "member-cleanup activity plan was replaced, expired, or finished"
            )
        return state

    @staticmethod
    def _session_id(plan: PlanReference) -> int:
        if isinstance(plan, MemberActivityPlan):
            return plan.session.session_id
        return plan.session_id

    @staticmethod
    def _same_session(state: _PlanState, plan: PlanReference) -> bool:
        session = plan.session if isinstance(plan, MemberActivityPlan) else plan
        return state.session == session

    @staticmethod
    def _snapshot(state: _PlanState) -> MemberActivityPlan:
        if state.confirmation_code is None or state.candidate_ids is None:
            raise MemberActivityPlanNotFound("member-cleanup activity plan is not bound")
        return MemberActivityPlan(
            session=state.session,
            confirmation_code=state.confirmation_code,
            candidate_ids=state.candidate_ids,
            expires_at=state.expires_at,
            executing=state.executing,
        )

    def _discard(self, session_id: int) -> None:
        state = self._states.pop(session_id, None)
        if state is None:
            return
        owner_key = (state.session.actor_id, state.session.chat_id)
        if self._by_owner_chat.get(owner_key) == session_id:
            self._by_owner_chat.pop(owner_key, None)
        chat_sessions = self._by_chat.get(state.session.chat_id)
        if chat_sessions is not None:
            chat_sessions.discard(session_id)
            if not chat_sessions:
                self._by_chat.pop(state.session.chat_id, None)
        if state.confirmation_code is not None:
            key = (state.session.actor_id, state.confirmation_code)
            if self._by_confirmation.get(key) == session_id:
                self._by_confirmation.pop(key, None)

    @staticmethod
    def _normalize_code(value: str) -> str:
        code = value.strip().upper()
        if not code:
            raise ValueError("confirmation_code must not be empty")
        return code

    @staticmethod
    def _as_utc(value: datetime, *, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _validate_ttl(value: float) -> None:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
