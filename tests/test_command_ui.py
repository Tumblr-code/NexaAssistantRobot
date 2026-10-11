from pathlib import Path

from nexa_guardian.command_ui import (
    COMMAND_SPECS,
    TOP_LEVEL_COMMANDS,
    bot_commands,
    help_text,
)

EXPECTED_COMMANDS = (
    ("help", "查看极简使用说明"),
    ("status", "查看运行状态与管理统计"),
    ("del", "删除消息、群历史或成员"),
    ("ban", "回复消息或填写用户 ID 封禁"),
)


def test_command_specs_are_the_single_five_command_source() -> None:
    assert tuple((spec.name, spec.description) for spec in COMMAND_SPECS) == EXPECTED_COMMANDS
    assert TOP_LEVEL_COMMANDS == tuple(name for name, _description in EXPECTED_COMMANDS)
    assert tuple((item.command, item.description) for item in bot_commands()) == EXPECTED_COMMANDS

    rendered_help = help_text()
    for spec in COMMAND_SPECS:
        for line in spec.help_lines:
            assert line in rendered_help


def test_readme_command_table_matches_command_specs() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    for name, description in EXPECTED_COMMANDS:
        assert f"| `/{name}` | {description} |" in readme
