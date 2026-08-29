import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nexa_guardian import main as main_module


class FakeStorage:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.pruned: list[int] = []

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def prune_ai_decisions(self, *, retention_days: int) -> int:
        self.pruned.append(retention_days)
        return 0


class FakeSession:
    def __init__(self) -> None:
        self.registered: list[object] = []
        self.closed = False

    def middleware(self, value: object) -> object:
        self.registered.append(value)
        return value

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    def __init__(self, webhook_url: str = "") -> None:
        self.session = FakeSession()
        self.webhook_url = webhook_url

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=1, username="NexaAdminRobot")

    async def get_webhook_info(self) -> SimpleNamespace:
        return SimpleNamespace(url=self.webhook_url)


class FakeEphemeral:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.stopped = True


class FakeController:
    def __init__(self) -> None:
        self.router = object()
        self.commands_set = False
        self.stopped = False

    async def set_commands(self) -> None:
        self.commands_set = True

    async def shutdown(self) -> None:
        self.stopped = True


class FakeDispatcher:
    def __init__(self) -> None:
        self.router: object | None = None
        self.polled = False

    def include_router(self, router: object) -> None:
        self.router = router

    def resolve_used_update_types(self) -> list[str]:
        return ["message"]

    async def start_polling(
        self,
        bot: object,
        *,
        allowed_updates: list[str],
        close_bot_session: bool,
    ) -> None:
        assert allowed_updates == ["message"]
        assert close_bot_session is False
        self.polled = True


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        log_level="INFO",
        database_path=Path("guardian.db"),
        bot_token="123:ABC",  # noqa: S106 - inert test fixture
        notice_ttl_seconds=30,
        ai_decision_retention_days=30,
        allowed_chat_ids=frozenset({-100}),
        dry_run=False,
    )


async def test_run_starts_and_closes_every_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    storage = FakeStorage()
    bot = FakeBot()
    ephemeral = FakeEphemeral()
    controller = FakeController()
    dispatcher = FakeDispatcher()
    configure = Mock()

    monkeypatch.setattr(main_module, "Settings", SimpleNamespace(from_env=lambda: settings))
    monkeypatch.setattr(main_module, "configure_logging", configure)
    monkeypatch.setattr(main_module, "Storage", lambda path: storage)
    monkeypatch.setattr(main_module, "Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr(main_module, "EphemeralMessenger", lambda *args, **kwargs: ephemeral)
    monkeypatch.setattr(main_module, "EphemeralRequestMiddleware", lambda value: value)
    monkeypatch.setattr(main_module, "Dispatcher", lambda: dispatcher)
    monkeypatch.setattr(main_module, "ModerationController", lambda *args: controller)

    await main_module.run()

    configure.assert_called_once_with("INFO")
    assert storage.opened and storage.closed
    assert storage.pruned == [30]
    assert ephemeral.started and ephemeral.stopped
    assert controller.commands_set and controller.stopped
    assert dispatcher.router is controller.router
    assert dispatcher.polled
    assert bot.session.registered == [ephemeral]
    assert bot.session.closed


async def test_run_rejects_existing_webhook_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    storage = FakeStorage()
    bot = FakeBot("https://example.invalid/hook")
    ephemeral = FakeEphemeral()
    controller = FakeController()

    monkeypatch.setattr(main_module, "Settings", SimpleNamespace(from_env=lambda: settings))
    monkeypatch.setattr(main_module, "configure_logging", Mock())
    monkeypatch.setattr(main_module, "Storage", lambda path: storage)
    monkeypatch.setattr(main_module, "Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr(main_module, "EphemeralMessenger", lambda *args, **kwargs: ephemeral)
    monkeypatch.setattr(main_module, "EphemeralRequestMiddleware", lambda value: value)
    monkeypatch.setattr(main_module, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(main_module, "ModerationController", lambda *args: controller)

    with pytest.raises(RuntimeError, match="webhook"):
        await main_module.run()

    assert storage.closed
    assert ephemeral.stopped
    assert controller.stopped
    assert bot.session.closed


async def test_ai_decision_retention_runs_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main_module.prune_ai_decisions_periodically(storage, retention_days=30)

    assert storage.pruned == [30]


def test_main_reports_unexpected_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "run", lambda: object())
    monkeypatch.setattr(
        main_module.asyncio,
        "run",
        Mock(side_effect=RuntimeError("boom")),
    )
    exit_mock = Mock(side_effect=SystemExit(1))
    monkeypatch.setattr(main_module.sys, "exit", exit_mock)

    with pytest.raises(SystemExit):
        main_module.main()

    exit_mock.assert_called_once_with(1)
