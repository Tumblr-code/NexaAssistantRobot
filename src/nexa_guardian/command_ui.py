from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import BotCommand


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    description: str
    help_lines: tuple[str, ...]


COMMAND_SPECS = (
    CommandSpec(
        "help",
        "查看极简使用说明",
        ("<code>/help</code>　查看本说明",),
    ),
    CommandSpec(
        "status",
        "查看运行状态与管理统计",
        (
            "<code>/status</code>　运行状态与管理统计",
            "<code>/status ai</code>　私聊管理 AI 审核",
        ),
    ),
    CommandSpec(
        "del",
        "删除消息、群历史或成员",
        (
            "<code>/del</code>　回复消息后删除",
            "<code>/del all</code>　清理全部群历史",
            "<code>/del members</code>　私聊预览长期不活跃成员",
            "<code>/del members 确认码 confirm</code>　确认移出候选成员",
        ),
    ),
    CommandSpec(
        "ban",
        "回复消息或填写用户 ID 封禁",
        ("<code>/ban</code>　回复成员消息后封禁",),
    ),
    CommandSpec(
        "lottery",
        "创建或管理关键词抽奖",
        (
            "<code>/lottery 30m 1 参与 奖品</code>　创建关键词抽奖",
            "<code>/lottery end|cancel|reroll</code>　管理抽奖",
        ),
    ),
)

TOP_LEVEL_COMMANDS = tuple(spec.name for spec in COMMAND_SPECS)


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command=spec.name, description=spec.description) for spec in COMMAND_SPECS]


def help_text() -> str:
    lines = [
        "<b>Nexa Guardian</b>",
        "",
        "广告由 GPT-5.6 Luna 自动审核；累计 3 次警告自动封禁。",
        "",
    ]
    for spec in COMMAND_SPECS:
        lines.extend(spec.help_lines)
    lines.extend(
        (
            "",
            "私聊仅唯一授权管理员可用；群内普通回复约 30 秒后清理。",
        )
    )
    return "\n".join(lines)
