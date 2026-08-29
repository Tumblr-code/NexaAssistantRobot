from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from dotenv import dotenv_values

from nexa_guardian.ai_review import AIReviewer, AIReviewResult, ResponseRequester

PROBE_TEXT = "Benign capability probe for a Telegram moderation gateway."
PROBE_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def probe_gateway(
    *,
    base_url: str,
    api_key: str,
    model: str,
    requester: ResponseRequester | None = None,
) -> AIReviewResult:
    """Verify Responses, strict structured output, and image input without group data."""

    if model != "gpt-5.6":
        raise ValueError("AI_MODEL must be exactly gpt-5.6")
    reviewer = AIReviewer(
        base_url=base_url,
        api_key=api_key,
        model=model,
        requester=requester,
        timeout=8,
        concurrency=1,
        cache_ttl=0,
        cache_size=0,
    )
    return await reviewer.review(
        text=PROBE_TEXT,
        image_data_url=PROBE_IMAGE_DATA_URL,
        safety_identifier="gateway-capability-probe",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe the configured GPT-5.6 gateway")
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
    if _env_value(values, "AI_ENABLED", "false").casefold() not in true_values:
        raise ValueError("AI_ENABLED must be true for production")
    if _env_value(values, "AI_FAILURE_DELETE", "true").casefold() not in true_values:
        raise ValueError("AI_FAILURE_DELETE must be true for production")
    if _env_value(values, "DRY_RUN", "false").casefold() not in false_values:
        raise ValueError("DRY_RUN must be false for production")

    integer_settings = {
        "WARN_LIMIT": (3, "3"),
        "NOTICE_TTL_SECONDS": (30, "30"),
        "AI_CONCURRENCY": (8, "8"),
        "AI_CACHE_TTL_SECONDS": (600, "600"),
        "AI_CACHE_MAX_ENTRIES": (1000, "1000"),
        "AI_DECISION_RETENTION_DAYS": (30, "30"),
        "AI_IMAGE_MAX_BYTES": (5 * 1024 * 1024, str(5 * 1024 * 1024)),
    }
    for name, (expected, default) in integer_settings.items():
        try:
            actual = int(_env_value(values, name, default))
        except ValueError as exc:
            raise ValueError(f"{name} must be {expected} for production") from exc
        if actual != expected:
            raise ValueError(f"{name} must be {expected} for production")

    float_settings = {
        "AI_AD_THRESHOLD": (0.92, "0.92"),
        "AI_LEARN_THRESHOLD": (0.97, "0.97"),
        "AI_TIMEOUT_SECONDS": (8.0, "8"),
    }
    for name, (expected, default) in float_settings.items():
        try:
            actual = float(_env_value(values, name, default))
        except ValueError as exc:
            raise ValueError(f"{name} must be {expected:g} for production") from exc
        if actual != expected:
            raise ValueError(f"{name} must be {expected:g} for production")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.env_file.is_file() or arguments.env_file.is_symlink():
        print("gateway probe failed: protected environment file is unavailable", file=sys.stderr)
        return 1
    # Read only the exact protected file; inherited process variables cannot satisfy it.
    values = dotenv_values(arguments.env_file, interpolate=False)
    base_url = _env_value(values, "OPENAI_BASE_URL")
    api_key = _env_value(values, "OPENAI_API_KEY")
    model = _env_value(values, "AI_MODEL", "gpt-5.6")
    if not base_url or not api_key:
        print("gateway probe failed: gateway credentials are not configured", file=sys.stderr)
        return 1
    try:
        if arguments.require_production_settings:
            _validate_production_settings(values)
        result = asyncio.run(probe_gateway(base_url=base_url, api_key=api_key, model=model))
    except Exception as exc:
        print(f"gateway probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        "gateway probe ok: model=gpt-5.6 structured_output=true "
        f"image_input=true latency_ms={result.latency_ms}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
