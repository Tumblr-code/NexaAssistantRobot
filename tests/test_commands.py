from types import SimpleNamespace

from nexa_guardian.commands import command_arguments, command_payload, resolve_target


def message(text: str, *, reply_user_id: int | None = None):
    reply = (
        SimpleNamespace(from_user=SimpleNamespace(id=reply_user_id))
        if reply_user_id is not None
        else None
    )
    return SimpleNamespace(text=text, reply_to_message=reply)


def test_command_payload_and_arguments_keep_live_subcommands() -> None:
    value = message("/del members ABC123 confirm")
    assert command_payload(value) == "members ABC123 confirm"
    assert command_arguments(value) == ("members", "ABC123", "confirm")
    assert command_payload(message("/status")) == ""


def test_private_target_keeps_legacy_trailing_text_for_compatibility() -> None:
    target = resolve_target(message("/ban 42 旧版尾部文本"))
    assert target.user_id == 42
    assert target.arguments == ("旧版尾部文本",)
