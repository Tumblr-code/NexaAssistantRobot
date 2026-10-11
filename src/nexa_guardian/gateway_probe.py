from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from dotenv import dotenv_values

from nexa_guardian.ai_contract import (
    AI_API_STYLE,
    AI_MODEL,
    AI_REASONING_EFFORT,
    AI_REASONING_EFFORTS,
    AI_SERVICE_TIER,
    AI_STRUCTURED_OUTPUT,
    AI_TIMEOUT_SECONDS,
)
from nexa_guardian.ai_review import (
    AIReviewer,
    AIReviewHTTPError,
    AIReviewResult,
    ResponseRequester,
)

PROBE_TEXT = "Benign capability probe for a Telegram moderation gateway."
GATEWAY_PROBE_TIMEOUT_SECONDS = AI_TIMEOUT_SECONDS
PROBE_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def probe_gateway(
    *,
    base_url: str,
    api_key: str,
    model: str,
    reasoning_effort: str = AI_REASONING_EFFORT,
    service_tier: str = AI_SERVICE_TIER,
    api_style: str = AI_API_STYLE,
    structured_output: str = AI_STRUCTURED_OUTPUT,
    strict_response: bool = False,
    requester: ResponseRequester | None = None,
) -> AIReviewResult:
    """Verify the configured API style, structured output and image input without group data."""

    if not model.strip():
        raise ValueError("AI_MODEL must be configured")
    if reasoning_effort not in AI_REASONING_EFFORTS:
        raise ValueError("AI_REASONING_EFFORT is not a supported value")

    reviewer = AIReviewer(
        base_url=base_url,
        api_key=api_key,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        api_style=api_style,
        structured_output=structured_output,
        strict_response=strict_response,
        requester=requester,
        timeout=GATEWAY_PROBE_TIMEOUT_SECONDS,
        concurrency=1,
        cache_ttl=0,
        cache_size=0,
    )
    try:
        return await reviewer.review(
            text=PROBE_TEXT,
            image_data_url=PROBE_IMAGE_DATA_URL,
            safety_identifier="gateway-capability-probe",
            reasoning_effort=reasoning_effort,
        )
    finally:
        await reviewer.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe the configured AI review gateway")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/etc/nexa-guardian/guardian.env"),
        help="protected environment file to read",
    )
    parser.add_argument(
        "--require-production-settings",
        action="store_true",
        help="also require AI enabled, warning limit 3, 30-second notices, and failure deletion",
    )
    parser.add_argument(
        "--allow-model-cooldown",
        action="store_true",
        help="accept only an exact HTTP 429 model_cooldown after production settings validate",
    )
    return parser


def _env_value(values: Mapping[str, str | None], name: str, default: str = "") -> str:
    value = values.get(name)
    return default if value is None else value.strip()


def _validate_production_settings(values: Mapping[str, str | None]) -> None:
    true_values = {"true", "1", "yes", "on"}
    false_values = {"false", "0", "no", "off"}
    for name in ("AI_ENABLED", "AI_FAILURE_DELETE", "DRY_RUN"):
        if name not in values or not _env_value(values, name):
            raise ValueError(f"{name} must be explicitly configured for production")
    if _env_value(values, "AI_ENABLED", "false").casefold() not in true_values:
        raise ValueError("AI_ENABLED must be true for production")
    if _env_value(values, "AI_FAILURE_DELETE", "true").casefold() not in true_values:
        raise ValueError("AI_FAILURE_DELETE must be true for production")
    if _env_value(values, "DRY_RUN", "false").casefold() not in false_values:
        raise ValueError("DRY_RUN must be false for production")
    # The provider is the operator's choice; production only requires it to be spelled out.
    for name in ("OPENAI_BASE_URL", "AI_MODEL"):
        if name not in values or not _env_value(values, name):
            raise ValueError(f"{name} must be explicitly configured for production")
    for name in ("AI_REASONING_EFFORT", "AI_SERVICE_TIER"):
        if name not in values:
            raise ValueError(f"{name} must be explicitly configured for production")
    if _env_value(values, "AI_REASONING_EFFORT") not in AI_REASONING_EFFORTS:
        raise ValueError("AI_REASONING_EFFORT is not a supported value")

    # Thresholds and limits are the operator's policy; production requires each one to be
    # written down and well-formed rather than left to a default.
    integer_settings = (
        "UPDATE_CONCURRENCY_LIMIT",
        "WARN_LIMIT",
        "NOTICE_TTL_SECONDS",
        "AI_CONCURRENCY",
        "AI_CACHE_TTL_SECONDS",
        "AI_CACHE_MAX_ENTRIES",
        "AI_DECISION_RETENTION_DAYS",
        "AI_IMAGE_MAX_BYTES",
        "AI_IMAGE_TARGET_LONG_EDGE",
        "AI_MEDIA_CONCURRENCY",
    )
    for name in integer_settings:
        if name not in values or not _env_value(values, name):
            raise ValueError(f"{name} must be explicitly configured for production")
        try:
            valid = int(_env_value(values, name)) > 0
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{name} must be a positive integer for production")

    float_settings = (
        "AI_AD_THRESHOLD",
        "AI_LEARN_THRESHOLD",
        "AI_TIMEOUT_SECONDS",
        "AI_NORMAL_ACCEPT_CONFIDENCE",
        "AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS",
    )
    for name in float_settings:
        if name not in values or not _env_value(values, name):
            raise ValueError(f"{name} must be explicitly configured for production")
        try:
            valid = float(_env_value(values, name)) > 0
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{name} must be a positive number for production")

    expected_bot_id = _env_value(values, "EXPECTED_BOT_ID")
    try:
        parsed_bot_id = int(expected_bot_id)
    except ValueError as exc:
        raise ValueError("EXPECTED_BOT_ID must be a positive integer for production") from exc
    if parsed_bot_id <= 0:
        raise ValueError("EXPECTED_BOT_ID must be a positive integer for production")
    expected_username = _env_value(values, "EXPECTED_BOT_USERNAME").removeprefix("@")
    if not expected_username:
        raise ValueError("EXPECTED_BOT_USERNAME must be explicitly configured for production")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.allow_model_cooldown and not arguments.require_production_settings:
        print(
            "gateway probe failed: model cooldown exception requires production validation",
            file=sys.stderr,
        )
        return 1
    if not arguments.env_file.is_file() or arguments.env_file.is_symlink():
        print("gateway probe failed: protected environment file is unavailable", file=sys.stderr)
        return 1
    # Read only the exact protected file; inherited process variables cannot satisfy it.
    values = dotenv_values(arguments.env_file, interpolate=False)
    base_url = _env_value(values, "OPENAI_BASE_URL")
    api_key = _env_value(values, "OPENAI_API_KEY")
    model = _env_value(values, "AI_MODEL", AI_MODEL)
    reasoning_effort = _env_value(values, "AI_REASONING_EFFORT", AI_REASONING_EFFORT)
    service_tier = _env_value(values, "AI_SERVICE_TIER", AI_SERVICE_TIER)
    api_style = _env_value(values, "AI_API_STYLE", AI_API_STYLE).casefold()
    structured_output = _env_value(values, "AI_STRUCTURED_OUTPUT", AI_STRUCTURED_OUTPUT).casefold()
    strict_response = _env_value(values, "AI_STRICT_RESPONSE", "false").casefold() in {
        "true",
        "1",
        "yes",
        "on",
    }
    if not base_url or not api_key:
        print("gateway probe failed: gateway credentials are not configured", file=sys.stderr)
        return 1
    try:
        if arguments.require_production_settings:
            _validate_production_settings(values)
        efforts = (reasoning_effort,)

        async def run_probes() -> tuple[AIReviewResult, ...]:
            return tuple(
                [
                    await probe_gateway(
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        reasoning_effort=effort,
                        service_tier=service_tier,
                        api_style=api_style,
                        structured_output=structured_output,
                        strict_response=strict_response,
                    )
                    for effort in efforts
                ]
            )

        results = asyncio.run(run_probes())
    except AIReviewHTTPError as exc:
        if (
            arguments.allow_model_cooldown
            and exc.status == 429
            and exc.error_code == "model_cooldown"
        ):
            print(
                "gateway probe accepted operator-authorized temporary model_cooldown",
                file=sys.stderr,
            )
            return 0
        print(f"gateway probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"gateway probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        f"gateway probe ok: model={model} api_style={api_style} "
        f"reasoning={','.join(result.reasoning_effort for result in results)} "
        f"service_tier={','.join(result.actual_service_tier for result in results)} "
        "structured_output=true image_input=true "
        f"latency_ms={','.join(str(result.latency_ms) for result in results)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
