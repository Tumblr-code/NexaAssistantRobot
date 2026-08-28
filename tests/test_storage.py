from pathlib import Path

import pytest

from nexa_guardian.storage import Storage


@pytest.fixture
async def storage(tmp_path: Path):
    instance = Storage(tmp_path / "guardian.db")
    await instance.open()
    yield instance
    await instance.close()


async def test_warning_increment_and_clear(storage: Storage) -> None:
    assert await storage.warning_count(-100, 42) == 0
    assert await storage.add_warning(-100, 42) == 1
    assert await storage.add_warning(-100, 42) == 2
    assert await storage.warning_count(-100, 42) == 2
    await storage.clear_warnings(-100, 42)
    assert await storage.warning_count(-100, 42) == 0


async def test_trusted_users_are_scoped_per_chat(storage: Storage) -> None:
    await storage.add_trusted_user(-100, 42, 7)
    assert await storage.is_trusted(-100, 42)
    assert not await storage.is_trusted(-200, 42)
    await storage.remove_trusted_user(-100, 42)
    assert not await storage.is_trusted(-100, 42)


async def test_domains_and_words_are_scoped_per_chat(storage: Storage) -> None:
    await storage.add_allowed_domain(-100, "example.com", 7)
    await storage.add_blocked_word(-100, "加微信", 7)
    assert await storage.allowed_domains(-100) == {"example.com"}
    assert await storage.blocked_words(-100) == {"加微信"}
    assert await storage.allowed_domains(-200) == set()
    await storage.remove_allowed_domain(-100, "example.com")
    await storage.remove_blocked_word(-100, "加微信")
    assert await storage.allowed_domains(-100) == set()
    assert await storage.blocked_words(-100) == set()


async def test_batch_words_are_written_and_removed_together(storage: Storage) -> None:
    await storage.add_blocked_words(-100, ("词语一", "guaranteed profit"), 7)
    assert await storage.blocked_words(-100) == {"词语一", "guaranteed profit"}
    await storage.remove_blocked_words(-100, ("词语一", "guaranteed profit"))
    assert await storage.blocked_words(-100) == set()


async def test_action_stats(storage: Storage) -> None:
    await storage.record_action(chat_id=-100, action="delete")
    await storage.record_action(chat_id=-100, action="warn")
    await storage.record_action(chat_id=-100, action="mute")
    await storage.record_action(chat_id=-100, action="auto_mute")
    await storage.record_action(chat_id=-100, action="ban")
    await storage.record_action(chat_id=-200, action="delete")
    stats = await storage.chat_stats(-100)
    assert stats.deleted == 1
    assert stats.warned == 1
    assert stats.muted == 2
    assert stats.banned == 1
    assert stats.total_actions == 5
    row = await storage.db.execute_fetchall(
        "SELECT reason FROM moderation_log WHERE chat_id = ?",
        (-100,),
    )
    assert {item["reason"] for item in row} == {""}


async def test_time_filtered_stats_warning_totals_and_recent_actions(storage: Storage) -> None:
    await storage.record_action(chat_id=-100, action="delete", target_id=41)
    await storage.record_action(chat_id=-100, action="warn", actor_id=7, target_id=42)
    await storage.db.execute(
        "UPDATE moderation_log SET created_at = ? WHERE target_id = ?",
        ("2026-08-20T00:00:00+00:00", 41),
    )
    await storage.db.commit()
    await storage.add_warning(-100, 42)
    await storage.add_warning(-100, 42)
    await storage.add_warning(-100, 43)

    recent_stats = await storage.chat_stats(-100, since="2026-08-26T00:00:00+00:00")
    warning_stats = await storage.warning_stats(-100)
    actions = await storage.recent_actions(-100, limit=1)

    assert recent_stats.deleted == 0
    assert recent_stats.warned == 1
    assert warning_stats.users == 2
    assert warning_stats.total == 3
    assert len(actions) == 1
    assert actions[0].action == "warn"
    assert actions[0].actor_id == 7
    assert actions[0].target_id == 42
