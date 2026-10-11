from pathlib import Path

import pytest

from nexa_guardian.ai_contract import (
    AI_MODEL,
    AI_REASONING_EFFORT,
    AI_SERVICE_TIER,
    AI_TIMEOUT_SECONDS,
    LOCAL_REVIEW_MODELS,
)
from nexa_guardian.config import Settings


def test_reference_defaults_and_local_review_paths() -> None:
    assert AI_MODEL == "deepseek-flash"
    assert AI_REASONING_EFFORT == "none"
    assert LOCAL_REVIEW_MODELS == {"local-signature", "local-media"}


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
        "AI_NORMAL_REASONING_EFFORT",
        "AI_SUSPICIOUS_REASONING_EFFORT",
        "AI_NORMAL_ACCEPT_CONFIDENCE",
        "AI_SERVICE_TIER",
        "AI_AD_THRESHOLD",
        "AI_LEARN_THRESHOLD",
        "AI_TIMEOUT_SECONDS",
        "DEFAULT_MUTE_SECONDS",
        "NOTICE_TTL_SECONDS",
        "EXPECTED_BOT_ID",
        "EXPECTED_BOT_USERNAME",
        "UPDATE_CONCURRENCY_LIMIT",
        "AI_IMAGE_TARGET_LONG_EDGE",
        "AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS",
        "AI_MEDIA_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_ai_is_disabled_safely_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    settings = Settings.from_env()
    assert settings.ai_enabled is False
    assert settings.ai_model == AI_MODEL
    assert settings.ai_reasoning_effort == AI_REASONING_EFFORT
    assert settings.ai_normal_reasoning_effort == AI_REASONING_EFFORT
    assert settings.ai_suspicious_reasoning_effort == AI_REASONING_EFFORT
    assert settings.ai_service_tier == AI_SERVICE_TIER
    assert settings.ai_timeout_seconds == AI_TIMEOUT_SECONDS
    assert settings.notice_ttl_seconds == 30
    assert settings.database_path == Path("data/test.db")


def test_ai_timeout_can_be_configured_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "15")

    assert Settings.from_env().ai_timeout_seconds == 15


def test_retired_default_mute_setting_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("DEFAULT_MUTE_SECONDS", "retired-value")

    settings = Settings.from_env()

    assert not hasattr(settings, "default_mute_seconds")


def test_adaptive_reasoning_and_runtime_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_BOT_ID", "8476280054")
    monkeypatch.setenv("EXPECTED_BOT_USERNAME", "@ExampleGuardBot")
    monkeypatch.setenv("AI_NORMAL_REASONING_EFFORT", "none")
    monkeypatch.setenv("AI_SUSPICIOUS_REASONING_EFFORT", "none")
    monkeypatch.setenv("AI_NORMAL_ACCEPT_CONFIDENCE", "0.9")
    monkeypatch.setenv("UPDATE_CONCURRENCY_LIMIT", "32")
    monkeypatch.setenv("AI_IMAGE_MAX_BYTES", "1572864")
    monkeypatch.setenv("AI_IMAGE_TARGET_LONG_EDGE", "1280")
    monkeypatch.setenv("AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("AI_MEDIA_CONCURRENCY", "4")

    settings = Settings.from_env()

    assert settings.expected_bot_id == 8476280054
    assert settings.expected_bot_username == "ExampleGuardBot"
    assert settings.ai_normal_reasoning_effort == "none"
    assert settings.ai_suspicious_reasoning_effort == "none"
    assert settings.ai_normal_accept_confidence == 0.9
    assert settings.update_concurrency_limit == 32
    assert settings.ai_image_max_bytes == 1572864
    assert settings.ai_image_target_long_edge == 1280
    assert settings.ai_image_download_timeout_seconds == 2
    assert settings.ai_media_concurrency == 4


def test_adaptive_reasoning_must_be_configured_as_a_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("AI_NORMAL_REASONING_EFFORT", "none")

    with pytest.raises(ValueError, match="必须同时配置"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    (
        ("EXPECTED_BOT_ID", "0", "正整数"),
        ("AI_NORMAL_REASONING_EFFORT", "ultra", "AI_NORMAL_REASONING_EFFORT"),
        ("AI_SUSPICIOUS_REASONING_EFFORT", "max", "AI_SUSPICIOUS_REASONING_EFFORT"),
    ),
)
def test_runtime_safety_configuration_is_validated(
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str,
    message: str,
) -> None:
    _base_env(monkeypatch)
    if setting.startswith("AI_") and "REASONING" in setting:
        monkeypatch.setenv("AI_NORMAL_REASONING_EFFORT", "none")
        monkeypatch.setenv("AI_SUSPICIOUS_REASONING_EFFORT", "none")
    monkeypatch.setenv(setting, value)

    with pytest.raises(ValueError, match=message):
        Settings.from_env()


def test_ai_requires_gateway_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        Settings.from_env()

    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Settings.from_env()

    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-credential")
    with pytest.raises(ValueError, match="AI_MODEL"):
        Settings.from_env()

    monkeypatch.setenv("AI_MODEL", "deepseek-flash")
    settings = Settings.from_env()
    assert settings.ai_enabled is True
    assert settings.openai_base_url == "https://api.deepseek.com"
    assert settings.ai_api_style == "responses"
    assert settings.ai_structured_output == "json_schema"
    assert settings.ai_strict_response is False


def test_learning_threshold_cannot_be_lower_than_action_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("AI_AD_THRESHOLD", "0.95")
    monkeypatch.setenv("AI_LEARN_THRESHOLD", "0.90")
    with pytest.raises(ValueError, match="不能低于"):
        Settings.from_env()


@pytest.mark.parametrize(
    "base_url",
    ("https://gateway.example/v1", "https://api.openai.com", "http://localhost:11434/v1"),
)
def test_any_https_or_local_gateway_is_accepted(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-credential")
    monkeypatch.setenv("AI_MODEL", "vendor/model-name:2")

    settings = Settings.from_env()

    assert settings.openai_base_url == base_url
    assert settings.ai_model == "vendor/model-name:2"


@pytest.mark.parametrize(
    "base_url",
    ("http://gateway.example/v1", "ftp://gateway.example", "https://user:pw@gateway.example"),
)
def test_gateway_url_must_be_https_without_embedded_credentials(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-credential")
    monkeypatch.setenv("AI_MODEL", "deepseek-flash")

    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("setting", "value", "attribute"),
    (
        ("AI_MODEL", "gpt-5.6", "ai_model"),
        ("AI_REASONING_EFFORT", "high", "ai_reasoning_effort"),
        ("AI_REASONING_EFFORT", "", "ai_reasoning_effort"),
        ("AI_SERVICE_TIER", "priority", "ai_service_tier"),
        ("AI_SERVICE_TIER", "", "ai_service_tier"),
        ("AI_API_STYLE", "chat_completions", "ai_api_style"),
        ("AI_STRUCTURED_OUTPUT", "json_object", "ai_structured_output"),
    ),
)
def test_ai_contract_is_configurable(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str, attribute: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(setting, value)

    assert getattr(Settings.from_env(), attribute) == value


@pytest.mark.parametrize(
    ("setting", "value"),
    (
        ("AI_MODEL", "bad model name"),
        ("AI_SERVICE_TIER", "Priority!"),
        ("AI_API_STYLE", "completions"),
        ("AI_STRUCTURED_OUTPUT", "xml"),
        ("OFFICIAL_SITES", "Example Store"),
        ("OFFICIAL_SITES", "Example Store|http://store.example.com"),
        ("BRAND_NAME", "x" * 33),
    ),
)
def test_invalid_customisation_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(setting, value)

    with pytest.raises(ValueError, match=setting):
        Settings.from_env()


def test_identity_defaults_are_neutral_and_customisable(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    for name in ("BRAND_NAME", "COMMUNITY_NAME", "OFFICIAL_SITES", "AI_MODEL_LABEL"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env().branding
    assert defaults.name == "Guardian"
    assert defaults.official_sites == ()
    assert defaults.reviewer_label == "AI"

    monkeypatch.setenv("BRAND_NAME", "Example Guardian")
    monkeypatch.setenv("COMMUNITY_NAME", "Example")
    monkeypatch.setenv("OFFICIAL_SITES", "Example Store|https://Store.Example.com/")
    monkeypatch.setenv("AI_MODEL_LABEL", "Example-1")
    monkeypatch.setenv("WARN_LIMIT", "5")
    custom = Settings.from_env().branding

    assert custom.name == "Example Guardian"
    assert custom.official_domains == {"store.example.com"}
    assert custom.official_sites[0].url == "https://Store.Example.com"
    assert custom.reviewer_label == "Example-1"
    assert custom.warn_limit == 5
