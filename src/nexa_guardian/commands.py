from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import Message, User


@dataclass(frozen=True, slots=True)
class CommandTarget:
    user_id: int
    user: User | None
    arguments: tuple[str, ...]


def command_arguments(message: Message) -> tuple[str, ...]:
    payload = command_payload(message)
    return tuple(payload.split()) if payload else ()


def command_payload(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def resolve_target(message: Message) -> CommandTarget:
    arguments = command_arguments(message)
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        return CommandTarget(reply.from_user.id, reply.from_user, arguments)
    if not arguments:
        raise ValueError("请回复目标成员的消息，或在命令后填写数字用户 ID")
    try:
        user_id = int(arguments[0])
    except ValueError as exc:
        raise ValueError("目标必须是数字用户 ID；更推荐直接回复目标消息") from exc
    if user_id <= 0:
        raise ValueError("用户 ID 必须是正整数")
    return CommandTarget(user_id, None, arguments[1:])
