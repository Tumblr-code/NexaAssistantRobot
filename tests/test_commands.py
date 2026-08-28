from types import SimpleNamespace

import pytest

from nexa_guardian.commands import batch_items, duration_from, resolve_target


def message(text: str, *, reply_user_id: int | None = None):
    reply = (
        SimpleNamespace(from_user=SimpleNamespace(id=reply_user_id))
        if reply_user_id is not None
        else None
    )
    return SimpleNamespace(text=text, reply_to_message=reply)


def test_batch_items_support_spaces_commas_newlines_and_quoted_phrases() -> None:
    value = message('/addword 词语一，"guaranteed profit",词语二\n词语三 词语四')
    assert batch_items(value) == (
        "词语一",
        "guaranteed profit",
        "词语二",
        "词语三",
        "词语四",
    )
    assert batch_items(message("/addword 号池 供应 合作 长期")) == (
        "号池",
        "供应",
        "合作",
        "长期",
    )


def test_batch_items_support_chinese_quotes_and_reject_unclosed_quotes() -> None:
    assert batch_items(message("/addword 号池 供应 “长期 合作”")) == (
        "号池",
        "供应",
        "长期 合作",
    )
    with pytest.raises(ValueError, match="引号未闭合"):
        batch_items(message('/addword 号池 "长期 合作'))


def test_batch_items_deduplicate_and_limit() -> None:
    assert batch_items(message("/addword 重复，重复")) == ("重复",)
    with pytest.raises(ValueError, match="最多"):
        batch_items(message("/addword " + "，".join(f"词语{item}" for item in range(21))))


def test_duration_ignores_legacy_reason_text() -> None:
    assert duration_from(("2h", "旧原因文本"), default_seconds=3600) == 7200
    assert duration_from(("旧原因文本",), default_seconds=3600) == 3600


def test_private_target_keeps_trailing_arguments_for_duration() -> None:
    target = resolve_target(message("/mute 42 2h 旧原因文本"))
    assert target.user_id == 42
    assert target.arguments == ("2h", "旧原因文本")
