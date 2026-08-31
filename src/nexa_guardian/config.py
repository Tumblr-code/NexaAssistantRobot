from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from nexa_guardian.ai_contract import (
    AI_MODEL,
    AI_REASONING_EFFORT,
    AI_SERVICE_TIER,
    AI_TIMEOUT_SECONDS,
)


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _int_csv(name: str, value: str | None) -> frozenset[int]:
    result: set[int] = set()
    for item in _csv(value):
        try:
            result.add(int(item))
        except ValueError as exc:
            raise ValueError(f"{name} 必须是英文逗号分隔的整数，错误值：{item}") from exc
    return frozenset(result)


def _integer(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw in (None, "") else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise ValueError(f"{name} 必须大于或等于 {minimum}")
    return value


def _optional_positive_integer(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是正整数") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw in (None, "") else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum}–{maximum} 之间")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    allowed_chat_ids: frozenset[int]
    authorized_admin_ids: frozenset[int]
    log_chat_id: int | None
    database_path: Path
    allowed_domains: tuple[str, ...]
    blocked_words: tuple[str, ...]
    block_external_links: bool
    block_forwards: bool
    block_channel_mentions: bool
    warn_limit: int
    notice_ttl_seconds: int
    flood_window_seconds: int
    flood_max_messages: int
    duplicate_window_seconds: int
    duplicate_max_messages: int
    dry_run: bool
    log_level: str
    expected_bot_id: int | None = None
    expected_bot_username: str = ""
    update_concurrency_limit: int = 32
    openai_base_url: str = ""
    openai_api_key: str = ""
    ai_model: str = AI_MODEL
    ai_reasoning_effort: str = AI_REASONING_EFFORT
    ai_normal_reasoning_effort: str = AI_REASONING_EFFORT
    ai_suspicious_reasoning_effort: str = AI_REASONING_EFFORT
    ai_normal_accept_confidence: float = 0.90
    ai_service_tier: str = AI_SERVICE_TIER
    ai_enabled: bool = False
    ai_ad_threshold: float = 0.92
    ai_learn_threshold: float = 0.97
    ai_timeout_seconds: float = AI_TIMEOUT_SECONDS
    ai_concurrency: int = 8
    ai_cache_ttl_seconds: int = 600
    ai_cache_max_entries: int = 1000
    ai_failure_delete: bool = True
    ai_decision_retention_days: int = 30
    ai_image_max_bytes: int = 5 * 1024 * 1024
    ai_image_target_long_edge: int = 1280
    ai_image_download_timeout_seconds: float = 2.0
    ai_media_concurrency: int = 4
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_session_path: Path = Path("/var/lib/nexa-guardian/mtproto/admin")

    @property
    def mtproto_ready(self) -> bool:
        return self.telegram_api_id is not None and bool(self.telegram_api_hash)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token or token.endswith(":replace_me"):
            raise ValueError("缺少有效的 BOT_TOKEN，请复制 .env.example 为 .env 后填写")

        log_chat_raw = os.getenv("LOG_CHAT_ID", "").strip()
        try:
            log_chat_id = int(log_chat_raw) if log_chat_raw else None
        except ValueError as exc:
            raise ValueError("LOG_CHAT_ID 必须是整数") from exc

        warn_limit = _integer("WARN_LIMIT", 3, minimum=1)
        ai_enabled = _boolean("AI_ENABLED", False)
        openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        ai_model = os.getenv("AI_MODEL", AI_MODEL).strip()
        ai_reasoning_effort = os.getenv("AI_REASONING_EFFORT", AI_REASONING_EFFORT).strip()
        normal_effort_raw = os.getenv("AI_NORMAL_REASONING_EFFORT")
        suspicious_effort_raw = os.getenv("AI_SUSPICIOUS_REASONING_EFFORT")
        if (normal_effort_raw is None) != (suspicious_effort_raw is None):
            raise ValueError(
                "AI_NORMAL_REASONING_EFFORT 与 AI_SUSPICIOUS_REASONING_EFFORT 必须同时配置"
            )
        ai_normal_reasoning_effort = (
            ai_reasoning_effort if normal_effort_raw is None else normal_effort_raw.strip()
        )
        ai_suspicious_reasoning_effort = (
            ai_reasoning_effort if suspicious_effort_raw is None else suspicious_effort_raw.strip()
        )
        ai_service_tier = os.getenv("AI_SERVICE_TIER", AI_SERVICE_TIER).strip()
        if ai_enabled and not openai_base_url:
            raise ValueError("AI_ENABLED=true 时必须配置 OPENAI_BASE_URL")
        if ai_enabled and not openai_api_key:
            raise ValueError("AI_ENABLED=true 时必须配置 OPENAI_API_KEY")
        if ai_model != AI_MODEL:
            raise ValueError(f"AI_MODEL 必须固定为 {AI_MODEL}")
        if ai_reasoning_effort != AI_REASONING_EFFORT:
            raise ValueError(f"AI_REASONING_EFFORT 必须固定为 {AI_REASONING_EFFORT}")
        if ai_normal_reasoning_effort not in {"low", "high"}:
            raise ValueError("AI_NORMAL_REASONING_EFFORT 只能是 low 或 high")
        if ai_suspicious_reasoning_effort != "high":
            raise ValueError("AI_SUSPICIOUS_REASONING_EFFORT 必须是 high")
        if ai_service_tier != AI_SERVICE_TIER:
            raise ValueError(f"AI_SERVICE_TIER 必须固定为 {AI_SERVICE_TIER}")

        ai_ad_threshold = _float("AI_AD_THRESHOLD", 0.92, minimum=0.0, maximum=1.0)
        ai_learn_threshold = _float("AI_LEARN_THRESHOLD", 0.97, minimum=0.0, maximum=1.0)
        if ai_learn_threshold < ai_ad_threshold:
            raise ValueError("AI_LEARN_THRESHOLD 不能低于 AI_AD_THRESHOLD")

        telegram_api_id = _optional_positive_integer("TELEGRAM_API_ID")
        telegram_api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        if (telegram_api_id is None) != (not telegram_api_hash):
            raise ValueError("TELEGRAM_API_ID 与 TELEGRAM_API_HASH 必须同时配置")

        return cls(
            bot_token=token,
            allowed_chat_ids=_int_csv("ALLOWED_CHAT_IDS", os.getenv("ALLOWED_CHAT_IDS")),
            authorized_admin_ids=_int_csv(
                "AUTHORIZED_ADMIN_IDS", os.getenv("AUTHORIZED_ADMIN_IDS")
            ),
            log_chat_id=log_chat_id,
            database_path=Path(os.getenv("DATABASE_PATH", "data/guardian.db")).expanduser(),
            allowed_domains=_csv(os.getenv("ALLOWED_DOMAINS")),
            blocked_words=_csv(os.getenv("BLOCKED_WORDS")),
            block_external_links=_boolean("BLOCK_EXTERNAL_LINKS", True),
            block_forwards=_boolean("BLOCK_FORWARDS", False),
            block_channel_mentions=_boolean("BLOCK_CHANNEL_MENTIONS", False),
            warn_limit=warn_limit,
            notice_ttl_seconds=_integer("NOTICE_TTL_SECONDS", 30),
            flood_window_seconds=_integer("FLOOD_WINDOW_SECONDS", 8, minimum=1),
            flood_max_messages=_integer("FLOOD_MAX_MESSAGES", 6, minimum=1),
            duplicate_window_seconds=_integer("DUPLICATE_WINDOW_SECONDS", 30, minimum=1),
            duplicate_max_messages=_integer("DUPLICATE_MAX_MESSAGES", 2, minimum=1),
            dry_run=_boolean("DRY_RUN", False),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            expected_bot_id=_optional_positive_integer("EXPECTED_BOT_ID"),
            expected_bot_username=os.getenv("EXPECTED_BOT_USERNAME", "").strip().removeprefix("@"),
            update_concurrency_limit=_integer("UPDATE_CONCURRENCY_LIMIT", 32, minimum=1),
            openai_base_url=openai_base_url,
            openai_api_key=openai_api_key,
            ai_model=ai_model,
            ai_reasoning_effort=ai_reasoning_effort,
            ai_normal_reasoning_effort=ai_normal_reasoning_effort,
            ai_suspicious_reasoning_effort=ai_suspicious_reasoning_effort,
            ai_normal_accept_confidence=_float(
                "AI_NORMAL_ACCEPT_CONFIDENCE", 0.90, minimum=0.5, maximum=1.0
            ),
            ai_service_tier=ai_service_tier,
            ai_enabled=ai_enabled,
            ai_ad_threshold=ai_ad_threshold,
            ai_learn_threshold=ai_learn_threshold,
            ai_timeout_seconds=_float(
                "AI_TIMEOUT_SECONDS", AI_TIMEOUT_SECONDS, minimum=1.0, maximum=60.0
            ),
            ai_concurrency=_integer("AI_CONCURRENCY", 8, minimum=1),
            ai_cache_ttl_seconds=_integer("AI_CACHE_TTL_SECONDS", 600, minimum=1),
            ai_cache_max_entries=_integer("AI_CACHE_MAX_ENTRIES", 1000, minimum=1),
            ai_failure_delete=_boolean("AI_FAILURE_DELETE", True),
            ai_decision_retention_days=_integer("AI_DECISION_RETENTION_DAYS", 30, minimum=1),
            ai_image_max_bytes=_integer("AI_IMAGE_MAX_BYTES", 5 * 1024 * 1024, minimum=1024),
            ai_image_target_long_edge=_integer("AI_IMAGE_TARGET_LONG_EDGE", 1280, minimum=320),
            ai_image_download_timeout_seconds=_float(
                "AI_IMAGE_DOWNLOAD_TIMEOUT_SECONDS", 2.0, minimum=0.25, maximum=30.0
            ),
            ai_media_concurrency=_integer("AI_MEDIA_CONCURRENCY", 4, minimum=1),
            telegram_api_id=telegram_api_id,
            telegram_api_hash=telegram_api_hash,
            telegram_session_path=Path(
                os.getenv(
                    "TELEGRAM_SESSION_PATH",
                    "/var/lib/nexa-guardian/mtproto/admin",
                )
            ).expanduser(),
        )
