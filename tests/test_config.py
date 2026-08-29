from pathlib import Path

import pytest

from nexa_guardian.ai_contract import AI_MODEL, AI_REASONING_EFFORT, AI_SERVICE_TIER
from nexa_guardian.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "-100")
    monkeypatch.setenv("DATABASE_PATH", "data/test.db")
    for name in (
        "AI_ENABLED",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "AI_MODEL",
        "AI_REASONING_EFFORT",
        "AI_SERVICE_TIER",
        "AI_AD_THRESHOLD",
        "AI_LEARN_THRESHOLD",
        "NOTICE_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_ai_is_disabled_safely_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    settings = Settings.from_env()
    assert settings.ai_enabled is False
    assert settings.ai_model == AI_MODEL
    assert settings.ai_reasoning_effort == AI_REASONING_EFFORT
    assert settings.ai_service_tier == AI_SERVICE_TIER
    assert settings.notice_ttl_seconds == 30
    assert settings.database_path == Path("data/test.db")


def test_ai_requires_gateway_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        Settings.from_env()

    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example/v1/")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Settings.from_env()

    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-credential")
    settings = Settings.from_env()
    assert settings.ai_enabled is True
    assert settings.openai_base_url == "https://gateway.example/v1"


def test_learning_threshold_cannot_be_lower_than_action_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("AI_AD_THRESHOLD", "0.95")
    monkeypatch.setenv("AI_LEARN_THRESHOLD", "0.90")
    with pytest.raises(ValueError, match="不能低于"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("setting", "value"),
    (
        ("AI_MODEL", "gpt-5.6"),
        ("AI_REASONING_EFFORT", "low"),
        ("AI_SERVICE_TIER", "priority"),
    ),
)
def test_ai_contract_cannot_be_substituted(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(setting, value)

    with pytest.raises(ValueError, match=setting):
        Settings.from_env()
