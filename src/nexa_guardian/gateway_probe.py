from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from dotenv import dotenv_values

from nexa_guardian.ai_contract import (
    AI_MODEL,
    AI_REASONING_EFFORT,
    AI_REASONING_EFFORTS,
    AI_SERVICE_TIER,
    AI_TIMEOUT_SECONDS,
)
from nexa_guardian.ai_review import (
    AIReviewer,
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
    requester: ResponseRequester | None = None,
) -> AIReviewResult:
    """Verify Responses, strict structured output, and image input without group data."""

    if model != AI_MODEL:
        raise ValueError(f"AI_MODEL must be exactly {AI_MODEL}")
    if reasoning_effort not in AI_REASONING_EFFORTS:
        raise ValueError("AI_REASONING_EFFORT must be low or high")
    if service_tier != AI_SERVICE_TIER:
        raise ValueError(f"AI_SERVICE_TIER must be exactly {AI_SERVICE_TIER}")

    reviewer = AIReviewer(
        base_url=base_url,
        api_key=api_key,
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
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
    parser = argparse.ArgumentParser(description="Probe the configured GPT-5.5 gateway")
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
    fixed_settings = {
        "AI_MODEL": AI_MODEL,
        "AI_REASONING_EFFORT": AI_REASONING_EFFORT,
        "AI_NORMAL_REASONING_EFFORT": "low",
        "AI_SUSPICIOUS_REASONING_EFFORT": "high",
        "AI_SERVICE_TIER": AI_SERVICE_TIER,
    }
    for name, expected in fixed_settings.items():
        if name not in values or not _env_value(values, name):
            raise ValueError(f"{name} must be explicitly configured for production")
        if _env_value(values, name) != expected:
            raise ValueError(f"{name} must be {expected} for production")

    integer_settings = {
        "UPDATE_CONCURRENCY_LIMIT": (32, "32"),
        "WARN_LIMIT": (3, "3"),
        "NOTICE_TTL_SECONDS": (30, "30"),
        "AI_CONCURRENCY": (8, "8"),
        "AI_CACHE_TTL_SECONDS": (600, "600"),
        "AI_CACHE_MAX_ENTRIES": (1000, "1000"),
        "AI_DECISION_RETENTION_DAYS": (30, "30"),
        "AI_IMAGE_MAX_BYTES": (1_572_864, "1572864"),
        "AI_IMAGE_TARGET_LONG_EDGE": (1280, "1280"),
        "AI_MEDIA_CONCURRENCY": (4, "4"),
    }
    for name, (expected, default) in integer_settings.items():
        if name not in values or not _env_value(values, name):
            raise ValueError(f"{name} must be explicitly configured for production")
        try:
            actual = int(_env_value(values, name, default))
        except ValueError as exc:
            raise ValueError(f"{name} must be {expected} for production") from exc
        if actual != expected:
            raise ValueError(f"{name} must be {expected} for production")

    float_settings = {
        "AI_AD_THRESHOLD": (0.92, "0.92"),
        "AI_LEARN_THRESHOLD": (0.97, "0.97"),
        "AI_TIMEOUT_SECONDS": (AI_TIMEOUT_SECONDS, f"{AI_TIMEOUT_SECONDS:g}"),
        "AI_NORMAL_ACCEPT_CONFIDENCE": (0.90, "0.90"),
        "AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS": (2.0, "2"),
    }
    for name, (expected, default) in float_settings.items():
        if name not in values or not _env_value(values, name):
            raise ValueError(f"{name} must be explicitly configured for production")
        try:
            actual = float(_env_value(values, name, default))
        except ValueError as exc:
            raise ValueError(f"{name} must be {expected:g} for production") from exc
        if actual != expected:
            raise ValueError(f"{name} must be {expected:g} for production")

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
    if not base_url or not api_key:
        print("gateway probe failed: gateway credentials are not configured", file=sys.stderr)
        return 1
    try:
        if arguments.require_production_settings:
            _validate_production_settings(values)
        efforts = ("low", "high") if arguments.require_production_settings else (reasoning_effort,)

        async def run_probes() -> tuple[AIReviewResult, ...]:
            return tuple(
                [
                    await probe_gateway(
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        reasoning_effort=effort,
                        service_tier=service_tier,
                    )
                    for effort in efforts
                ]
            )

        results = asyncio.run(run_probes())
    except Exception as exc:
        print(f"gateway probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        f"gateway probe ok: model={AI_MODEL} "
        f"reasoning={','.join(result.reasoning_effort for result in results)} "
        f"service_tier={','.join(result.actual_service_tier for result in results)} "
        "structured_output=true image_input=true "
        f"latency_ms={','.join(str(result.latency_ms) for result in results)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
