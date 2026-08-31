import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nexa_guardian import gateway_probe
from nexa_guardian.ai_contract import (
    AI_MODEL,
    AI_REASONING_EFFORT,
    AI_SERVICE_TIER,
    AI_TIMEOUT_SECONDS,
)
from nexa_guardian.ai_review import AIReviewResult, HTTPResponse
from nexa_guardian.gateway_probe import (
    GATEWAY_PROBE_TIMEOUT_SECONDS,
    PROBE_IMAGE_DATA_URL,
    PROBE_TEXT,
    probe_gateway,
)


def production_env(**overrides: str) -> str:
    values = {
        "OPENAI_BASE_URL": "https://protected.example/v1",
        "OPENAI_API_KEY": "protected-placeholder",
        "EXPECTED_BOT_ID": "123456789",
        "EXPECTED_BOT_USERNAME": "ExampleGuardBot",
        "UPDATE_CONCURRENCY_LIMIT": "32",
        "AI_MODEL": AI_MODEL,
        "AI_REASONING_EFFORT": AI_REASONING_EFFORT,
        "AI_NORMAL_REASONING_EFFORT": "low",
        "AI_SUSPICIOUS_REASONING_EFFORT": "high",
        "AI_SERVICE_TIER": AI_SERVICE_TIER,
        "AI_ENABLED": "true",
        "AI_FAILURE_DELETE": "true",
        "DRY_RUN": "false",
        "WARN_LIMIT": "3",
        "NOTICE_TTL_SECONDS": "30",
        "AI_CONCURRENCY": "8",
        "AI_CACHE_TTL_SECONDS": "600",
        "AI_CACHE_MAX_ENTRIES": "1000",
        "AI_DECISION_RETENTION_DAYS": "30",
        "AI_IMAGE_MAX_BYTES": "1572864",
        "AI_IMAGE_TARGET_LONG_EDGE": "1280",
        "AI_MEDIA_CONCURRENCY": "4",
        "AI_AD_THRESHOLD": "0.92",
        "AI_LEARN_THRESHOLD": "0.97",
        "AI_TIMEOUT_SECONDS": f"{AI_TIMEOUT_SECONDS:g}",
        "AI_NORMAL_ACCEPT_CONFIDENCE": "0.90",
        "AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS": "2",
    }
    values.update(overrides)
    return "".join(f"{name}={value}\n" for name, value in values.items())


@pytest.mark.parametrize("reasoning_effort", ("low", "high"))
async def test_gateway_probe_uses_fixed_model_structured_output_and_image(
    reasoning_effort: str,
) -> None:
    calls: list[dict[str, object]] = []

    async def requester(**kwargs: object) -> HTTPResponse:
        calls.append(kwargs)
        return HTTPResponse(
            200,
            {
                "status": "completed",
                "model": AI_MODEL,
                "reasoning": {"effort": reasoning_effort},
                "service_tier": AI_SERVICE_TIER,
                "output_text": json.dumps(
                    {
                        "is_ad": False,
                        "confidence": 0.99,
                        "category": "normal",
                        "signatures": [],
                    }
                ),
            },
        )

    result = await probe_gateway(
        base_url="https://gateway.example/v1",
        api_key="placeholder-credential",
        model=AI_MODEL,
        reasoning_effort=reasoning_effort,
        requester=requester,
    )

    assert result.is_ad is False
    payload = calls[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == AI_MODEL
    assert payload["reasoning"] == {"effort": reasoning_effort}
    assert payload["service_tier"] == AI_SERVICE_TIER
    assert payload["store"] is False
    assert result.reasoning_effort == reasoning_effort
    assert result.actual_service_tier == AI_SERVICE_TIER
    assert (
        GATEWAY_PROBE_TIMEOUT_SECONDS - 0.1 < calls[0]["timeout"] <= GATEWAY_PROBE_TIMEOUT_SECONDS
    )
    user_content = payload["input"][1]["content"]  # type: ignore[index]
    assert {item["type"] for item in user_content} == {"input_text", "input_image"}
    assert PROBE_TEXT in {item.get("text") for item in user_content}
    assert PROBE_IMAGE_DATA_URL in {item.get("image_url") for item in user_content}


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"model": "gpt-5.6"}, "AI_MODEL"),
        ({"reasoning_effort": "medium"}, "AI_REASONING_EFFORT"),
        ({"service_tier": "priority"}, "AI_SERVICE_TIER"),
    ),
)
async def test_gateway_probe_rejects_contract_substitution(
    overrides: dict[str, str], message: str
) -> None:
    values: dict[str, object] = {
        "base_url": "https://gateway.example/v1",
        "api_key": "placeholder-credential",
        "model": AI_MODEL,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        await probe_gateway(**values)  # type: ignore[arg-type]


def test_probe_cli_uses_exact_protected_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "guardian.env"
    env_file.write_text(
        "OPENAI_BASE_URL=https://protected.example/v1\n"
        "OPENAI_API_KEY=protected-placeholder\n"
        f"AI_MODEL={AI_MODEL}\n"
        f"AI_REASONING_EFFORT={AI_REASONING_EFFORT}\n"
        f"AI_SERVICE_TIER={AI_SERVICE_TIER}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-placeholder")

    async def fake_probe_gateway(**kwargs: object) -> AIReviewResult:
        assert kwargs["base_url"] == "https://protected.example/v1"
        assert kwargs["api_key"] == "protected-placeholder"
        assert kwargs["model"] == AI_MODEL
        assert kwargs["reasoning_effort"] == AI_REASONING_EFFORT
        assert kwargs["service_tier"] == AI_SERVICE_TIER
        return AIReviewResult(False, 1.0, "normal", (), False, 12)

    monkeypatch.setattr(gateway_probe, "probe_gateway", fake_probe_gateway)

    assert gateway_probe.main(["--env-file", str(env_file)]) == 0


def test_probe_cli_requires_live_production_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / "guardian.env"
    env_file.write_text(
        "OPENAI_BASE_URL=https://protected.example/v1\n"
        "OPENAI_API_KEY=protected-placeholder\n"
        f"AI_MODEL={AI_MODEL}\n"
        f"AI_REASONING_EFFORT={AI_REASONING_EFFORT}\n"
        f"AI_SERVICE_TIER={AI_SERVICE_TIER}\n"
        "AI_ENABLED=false\n",
        encoding="utf-8",
    )
    probe = AsyncMock()
    monkeypatch.setattr(gateway_probe, "probe_gateway", probe)

    assert gateway_probe.main(["--env-file", str(env_file), "--require-production-settings"]) == 1
    assert "ValueError" in capsys.readouterr().err
    probe.assert_not_awaited()


def test_probe_cli_accepts_explicit_required_production_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "guardian.env"
    env_file.write_text(production_env(), encoding="utf-8")
    efforts: list[str] = []

    async def fake_probe_gateway(**kwargs: object) -> AIReviewResult:
        effort = str(kwargs["reasoning_effort"])
        efforts.append(effort)
        return AIReviewResult(
            False,
            1.0,
            "normal",
            (),
            False,
            12,
            reasoning_effort=effort,
        )

    monkeypatch.setattr(gateway_probe, "probe_gateway", fake_probe_gateway)

    assert gateway_probe.main(["--env-file", str(env_file), "--require-production-settings"]) == 0
    assert efforts == ["low", "high"]


def test_probe_cli_does_not_accept_credentials_from_ambient_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / "guardian.env"
    env_file.write_text(f"AI_MODEL={AI_MODEL}\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-placeholder")
    monkeypatch.setenv("AI_ENABLED", "true")
    probe = AsyncMock()
    monkeypatch.setattr(gateway_probe, "probe_gateway", probe)

    assert gateway_probe.main(["--env-file", str(env_file)]) == 1
    assert "credentials are not configured" in capsys.readouterr().err
    probe.assert_not_awaited()


@pytest.mark.parametrize(
    ("setting", "value"),
    (
        ("AI_MODEL", "gpt-5.6"),
        ("AI_REASONING_EFFORT", "low"),
        ("AI_NORMAL_REASONING_EFFORT", "high"),
        ("AI_SUSPICIOUS_REASONING_EFFORT", "low"),
        ("AI_SERVICE_TIER", "priority"),
        ("EXPECTED_BOT_ID", "0"),
        ("EXPECTED_BOT_USERNAME", ""),
        ("UPDATE_CONCURRENCY_LIMIT", "31"),
        ("DRY_RUN", "true"),
        ("AI_FAILURE_DELETE", "false"),
        ("WARN_LIMIT", "4"),
        ("NOTICE_TTL_SECONDS", "29"),
        ("AI_AD_THRESHOLD", "0.93"),
        ("AI_LEARN_THRESHOLD", "0.98"),
        ("AI_TIMEOUT_SECONDS", "8"),
        ("AI_CONCURRENCY", "7"),
        ("AI_CACHE_TTL_SECONDS", "601"),
        ("AI_CACHE_MAX_ENTRIES", "999"),
        ("AI_DECISION_RETENTION_DAYS", "31"),
        ("AI_IMAGE_MAX_BYTES", "5242880"),
        ("AI_IMAGE_TARGET_LONG_EDGE", "1024"),
        ("AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS", "3"),
        ("AI_MEDIA_CONCURRENCY", "3"),
        ("AI_NORMAL_ACCEPT_CONFIDENCE", "0.91"),
    ),
)
def test_probe_cli_rejects_non_release_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str,
) -> None:
    env_file = tmp_path / "guardian.env"
    env_file.write_text(production_env(**{setting: value}), encoding="utf-8")
    probe = AsyncMock()
    monkeypatch.setattr(gateway_probe, "probe_gateway", probe)

    assert gateway_probe.main(["--env-file", str(env_file), "--require-production-settings"]) == 1
    probe.assert_not_awaited()


@pytest.mark.parametrize(
    "missing_setting",
    ("AI_MODEL", "AI_REASONING_EFFORT", "AI_SERVICE_TIER"),
)
def test_probe_cli_requires_explicit_model_contract_for_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_setting: str,
) -> None:
    env_file = tmp_path / "guardian.env"
    values = production_env().replace(
        f"{missing_setting}="
        + {
            "AI_MODEL": AI_MODEL,
            "AI_REASONING_EFFORT": AI_REASONING_EFFORT,
            "AI_SERVICE_TIER": AI_SERVICE_TIER,
        }[missing_setting]
        + "\n",
        "",
    )
    env_file.write_text(values, encoding="utf-8")
    probe = AsyncMock()
    monkeypatch.setattr(gateway_probe, "probe_gateway", probe)

    assert gateway_probe.main(["--env-file", str(env_file), "--require-production-settings"]) == 1
    probe.assert_not_awaited()
