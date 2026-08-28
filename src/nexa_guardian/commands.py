from __future__ import annotations

import shlex
from dataclasses import dataclass

from aiogram.types import Message, User

from nexa_guardian.detection import parse_duration


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


def batch_items(message: Message, *, limit: int = 20) -> tuple[str, ...]:
    payload = command_payload(message).translate(
        str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
    )
    lexer = shlex.shlex(payload, posix=True)
    lexer.commenters = ""
    lexer.whitespace += ",，"
    lexer.whitespace_split = True
    try:
        parsed = tuple(lexer)
    except ValueError as exc:
        raise ValueError("拦截词引号未闭合，请补全后重试") from exc
    items = tuple(dict.fromkeys(item.strip() for item in parsed if item.strip()))
    if len(items) > limit:
        raise ValueError(f"每次最多处理 {limit} 条")
    return items


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


def duration_from(arguments: tuple[str, ...], *, default_seconds: int) -> int:
    if not arguments:
        return default_seconds
    first = arguments[0]
    if first[:1].isdigit():
        return parse_duration(first)
    return default_seconds
