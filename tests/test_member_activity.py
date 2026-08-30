from datetime import datetime, timedelta, timezone

import pytest

from nexa_guardian.member_activity import (
    DEFAULT_MEMBER_ACTIVITY_TTL_SECONDS,
    MemberActivityPlanBusy,
    MemberActivityPlanNotFound,
    MemberActivityTracker,
)

UTC = timezone.utc
ACTOR_ID = 7
GROUP_ID = -100
CREATED_AT = datetime(2026, 8, 30, 12, tzinfo=UTC)
CUTOFF = CREATED_AT - timedelta(days=30)


def begin(tracker: MemberActivityTracker):
    return tracker.begin(
        actor_id=ACTOR_ID,
        chat_id=GROUP_ID,
        cutoff=CUTOFF,
        created_at=CREATED_AT,
    )


def test_tracks_scan_activity_and_narrows_to_bound_candidates() -> None:
    tracker = MemberActivityTracker()
    session = begin(tracker)

    assert tracker.note(GROUP_ID, 101, observed_at=CREATED_AT + timedelta(seconds=1))
    assert tracker.note(GROUP_ID, 102, observed_at=CREATED_AT + timedelta(seconds=2))

    plan = tracker.bind(
        session,
        confirmation_code="a1b2c3",
        candidate_ids=(101, 103),
    )
    assert plan.confirmation_code == "A1B2C3"
    assert plan.candidate_ids == frozenset({101, 103})
    assert tracker.active_ids(plan) == frozenset({101})

    assert not tracker.note(GROUP_ID, 102, observed_at=CREATED_AT + timedelta(seconds=3))
    assert tracker.note(GROUP_ID, 103, observed_at=CREATED_AT + timedelta(seconds=4))
    assert tracker.active_ids(plan) == frozenset({101, 103})


def test_ignores_activity_older_than_preview_and_other_chats() -> None:
    tracker = MemberActivityTracker()
    session = begin(tracker)
    plan = tracker.bind(session, confirmation_code="ABC123", candidate_ids=(101,))

    assert not tracker.note(
        GROUP_ID,
        101,
        observed_at=CREATED_AT - timedelta(microseconds=1),
    )
    assert not tracker.note(GROUP_ID - 1, 101, observed_at=CREATED_AT + timedelta(seconds=1))
    assert tracker.active_ids(plan) == frozenset()


def test_confirmation_lookup_is_single_use_but_execution_keeps_tracking() -> None:
    clock = {"now": 100.0}
    tracker = MemberActivityTracker(clock=lambda: clock["now"])
    session = begin(tracker)
    tracker.bind(session, confirmation_code="ABC123", candidate_ids=(101, 102))

    pending = tracker.get(actor_id=ACTOR_ID, confirmation_code="abc123")
    assert pending is not None and not pending.executing
    executing = tracker.start_execution(actor_id=ACTOR_ID, confirmation_code="ABC123")
    assert executing is not None and executing.executing
    assert tracker.start_execution(actor_id=ACTOR_ID, confirmation_code="ABC123") is None

    clock["now"] += DEFAULT_MEMBER_ACTIVITY_TTL_SECONDS + 1
    assert tracker.note(GROUP_ID, 101, observed_at=CREATED_AT + timedelta(minutes=11))
    assert tracker.active_ids(executing) == frozenset({101})
    assert tracker.finish(executing)
    assert not tracker.note(GROUP_ID, 102, observed_at=CREATED_AT + timedelta(minutes=12))
    assert not tracker.finish(executing)


def test_pending_plan_expires_after_ten_minutes() -> None:
    clock = {"now": 100.0}
    tracker = MemberActivityTracker(clock=lambda: clock["now"])
    session = begin(tracker)
    plan = tracker.bind(session, confirmation_code="ABC123", candidate_ids=(101,))

    assert plan.expires_at == 100.0 + DEFAULT_MEMBER_ACTIVITY_TTL_SECONDS
    clock["now"] = plan.expires_at
    assert tracker.get(actor_id=ACTOR_ID, confirmation_code="ABC123") is None
    with pytest.raises(MemberActivityPlanNotFound):
        tracker.active_ids(plan)
    assert tracker.expire() == 0


def test_confirmation_gets_full_ttl_after_a_slow_scan() -> None:
    clock = {"now": 100.0}
    tracker = MemberActivityTracker(clock=lambda: clock["now"])
    session = begin(tracker)
    clock["now"] = 219.0

    plan = tracker.bind(session, confirmation_code="ABC123", candidate_ids=(101,))

    assert plan.expires_at == 219.0 + DEFAULT_MEMBER_ACTIVITY_TTL_SECONDS
    clock["now"] = 818.0
    assert tracker.get(actor_id=ACTOR_ID, confirmation_code="ABC123") is not None
    clock["now"] = 819.0
    assert tracker.get(actor_id=ACTOR_ID, confirmation_code="ABC123") is None


def test_new_pending_plan_replaces_same_actor_and_chat_plan() -> None:
    tracker = MemberActivityTracker()
    old_session = begin(tracker)
    tracker.bind(old_session, confirmation_code="OLD123", candidate_ids=(101,))

    new_session = begin(tracker)
    tracker.bind(new_session, confirmation_code="NEW123", candidate_ids=(102,))

    assert tracker.get(actor_id=ACTOR_ID, confirmation_code="OLD123") is None
    assert tracker.get(actor_id=ACTOR_ID, confirmation_code="NEW123") is not None
    with pytest.raises(MemberActivityPlanNotFound):
        tracker.active_ids(old_session)


def test_scan_in_progress_blocks_concurrent_preview() -> None:
    tracker = MemberActivityTracker()
    session = begin(tracker)

    with pytest.raises(MemberActivityPlanBusy):
        begin(tracker)

    assert tracker.finish(session)
    begin(tracker)


def test_executing_plan_blocks_replacement_until_finished() -> None:
    tracker = MemberActivityTracker()
    session = begin(tracker)
    tracker.bind(session, confirmation_code="ABC123", candidate_ids=(101,))
    executing = tracker.start_execution(actor_id=ACTOR_ID, confirmation_code="ABC123")
    assert executing is not None

    with pytest.raises(MemberActivityPlanBusy):
        begin(tracker)
    assert tracker.finish(executing)
    begin(tracker)


def test_plans_for_different_actor_chat_pairs_are_independent() -> None:
    tracker = MemberActivityTracker()
    first = begin(tracker)
    second = tracker.begin(
        actor_id=ACTOR_ID + 1,
        chat_id=GROUP_ID,
        cutoff=CUTOFF,
        created_at=CREATED_AT,
    )
    first_plan = tracker.bind(first, confirmation_code="FIRST1", candidate_ids=(101,))
    second_plan = tracker.bind(second, confirmation_code="SECOND", candidate_ids=(102,))

    assert tracker.note(GROUP_ID, 101, observed_at=CREATED_AT)
    assert tracker.active_ids(first_plan) == frozenset({101})
    assert tracker.active_ids(second_plan) == frozenset()


@pytest.mark.parametrize("ttl", [0.0, -1.0, float("inf"), float("nan")])
def test_rejects_invalid_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        MemberActivityTracker(default_ttl_seconds=ttl)


def test_rejects_naive_timestamps() -> None:
    tracker = MemberActivityTracker()
    with pytest.raises(ValueError, match="created_at"):
        tracker.begin(
            actor_id=ACTOR_ID,
            chat_id=GROUP_ID,
            cutoff=CUTOFF,
            created_at=CREATED_AT.replace(tzinfo=None),
        )

    session = begin(tracker)
    with pytest.raises(ValueError, match="observed_at"):
        tracker.note(GROUP_ID, 101, observed_at=CREATED_AT.replace(tzinfo=None))
    assert tracker.finish(session)
