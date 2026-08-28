from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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
    default_mute_seconds: int
    notice_ttl_seconds: int
    flood_window_seconds: int
    flood_max_messages: int
    duplicate_window_seconds: int
    duplicate_max_messages: int
    dry_run: bool
    log_level: str

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
        mute_seconds = _integer("DEFAULT_MUTE_SECONDS", 3600, minimum=60)
        if mute_seconds > 366 * 24 * 60 * 60:
            raise ValueError("DEFAULT_MUTE_SECONDS 不能超过 366 天")

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
            default_mute_seconds=mute_seconds,
            notice_ttl_seconds=_integer("NOTICE_TTL_SECONDS", 10),
            flood_window_seconds=_integer("FLOOD_WINDOW_SECONDS", 8, minimum=1),
            flood_max_messages=_integer("FLOOD_MAX_MESSAGES", 6, minimum=1),
            duplicate_window_seconds=_integer("DUPLICATE_WINDOW_SECONDS", 30, minimum=1),
            duplicate_max_messages=_integer("DUPLICATE_MAX_MESSAGES", 2, minimum=1),
            dry_run=_boolean("DRY_RUN", False),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
