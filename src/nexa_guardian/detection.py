from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiogram.enums import MessageEntityType
from aiogram.types import MessageEntity

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
URL_RE = re.compile(r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/|wa\.me/)[^\s<>{}\[\]]+")
MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{4,31}\b")
PROMOTIONAL_MENTION_CUES = (
    "加我",
    "私聊我",
    "联系飞机",
    "飞机联系",
    "纸飞机",
    "进群",
    "交流群",
    "频道",
    "代理",
    "招商",
    "招募",
    "兼职",
    "日结",
    "刷单",
    "返利",
    "带单",
    "投资群",
    "信号群",
    "免费领取",
    "dmme",
    "contactme",
    "joinmygroup",
    "cryptosignal",
)


@dataclass(frozen=True, slots=True)
class DetectionInput:
    text: str
    entity_urls: tuple[str, ...] = ()
    is_forwarded: bool = False


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    detail: str


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).translate(ZERO_WIDTH).casefold().strip()


def compact_text(value: str) -> str:
    return "".join(character for character in normalize_text(value) if character.isalnum())


def normalize_domain(value: str) -> str | None:
    candidate = value.strip().casefold().rstrip(".")
    if not candidate:
        return None
    if "://" in candidate or "/" in candidate:
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        candidate = parsed.hostname or ""
    else:
        candidate = candidate.split(":", 1)[0]
    candidate = candidate.rstrip(".")
    if not candidate or " " in candidate:
        return None
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def domain_from_url(value: str) -> str | None:
    candidate = value.strip().rstrip(".,!?:;，。！？；：、）)]}")
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        return normalize_domain(urlsplit(candidate).hostname or "")
    except ValueError:
        return None


def is_allowed_domain(host: str, allowed_domains: set[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def _slice_utf16(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


def extract_entity_urls(text: str, entities: list[MessageEntity] | None) -> tuple[str, ...]:
    urls: list[str] = []
    for entity in entities or []:
        if entity.type == MessageEntityType.TEXT_LINK and entity.url:
            urls.append(entity.url)
        elif entity.type == MessageEntityType.URL:
            urls.append(_slice_utf16(text, entity.offset, entity.length))
    return tuple(urls)


class AdDetector:
    def __init__(
        self,
        *,
        block_external_links: bool,
        block_forwards: bool,
        block_channel_mentions: bool,
    ) -> None:
        self.block_external_links = block_external_links
        self.block_forwards = block_forwards
        self.block_channel_mentions = block_channel_mentions

    def detect(
        self,
        message: DetectionInput,
        *,
        allowed_domains: set[str],
        blocked_words: set[str],
    ) -> Violation | None:
        normalized_domains = {
            normalized
            for item in allowed_domains
            if (normalized := normalize_domain(item)) is not None
        }

        if self.block_forwards and message.is_forwarded:
            return Violation("forward", "禁止转发内容")

        normalized_text = normalize_text(message.text)
        compact = compact_text(normalized_text)
        for word in sorted(blocked_words, key=len, reverse=True):
            needle = compact_text(word)
            if needle and needle in compact:
                return Violation("blocked_word", f"命中广告词：{word}")

        if self.block_channel_mentions:
            mentions = MENTION_RE.findall(normalized_text)
            has_promotion_cue = any(
                compact_text(cue) in compact for cue in PROMOTIONAL_MENTION_CUES
            )
            if mentions and (has_promotion_cue or len(set(mentions)) >= 2):
                return Violation("mention", f"禁止推广账号：{mentions[0]}")

        if self.block_external_links:
            raw_urls = [
                *message.entity_urls,
                *(match.group(0) for match in URL_RE.finditer(normalized_text)),
            ]
            for raw_url in dict.fromkeys(raw_urls):
                host = domain_from_url(raw_url)
                if host is None or not is_allowed_domain(host, normalized_domains):
                    shown = host or raw_url[:80]
                    return Violation("external_link", f"非白名单外链：{shown}")

        return None


class FloodGuard:
    def __init__(
        self,
        *,
        window_seconds: int,
        max_messages: int,
        duplicate_window_seconds: int,
        duplicate_max_messages: int,
    ) -> None:
        self.window_seconds = window_seconds
        self.max_messages = max_messages
        self.duplicate_window_seconds = duplicate_window_seconds
        self.duplicate_max_messages = duplicate_max_messages
        self._messages: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._duplicates: dict[tuple[int, int, bytes], deque[float]] = defaultdict(deque)

    def check(
        self, chat_id: int, user_id: int, text: str, *, now: float | None = None
    ) -> Violation | None:
        timestamp = time.monotonic() if now is None else now
        key = (chat_id, user_id)
        messages = self._messages[key]
        self._prune(messages, timestamp - self.window_seconds)
        messages.append(timestamp)
        if len(messages) > self.max_messages:
            return Violation("flood", f"{self.window_seconds} 秒内消息过多")

        normalized = compact_text(text)
        if len(normalized) >= 4:
            digest = hashlib.blake2b(normalized.encode(), digest_size=8).digest()
            duplicate_key = (chat_id, user_id, digest)
            duplicates = self._duplicates[duplicate_key]
            self._prune(duplicates, timestamp - self.duplicate_window_seconds)
            duplicates.append(timestamp)
            if len(duplicates) > self.duplicate_max_messages:
                return Violation(
                    "duplicate", f"{self.duplicate_window_seconds} 秒内重复发送相同内容"
                )

        if len(self._messages) + len(self._duplicates) > 10_000:
            self._compact(timestamp)
        return None

    @staticmethod
    def _prune(queue: deque[float], cutoff: float) -> None:
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def _compact(self, now: float) -> None:
        message_cutoff = now - self.window_seconds
        duplicate_cutoff = now - self.duplicate_window_seconds
        for key, queue in list(self._messages.items()):
            self._prune(queue, message_cutoff)
            if not queue:
                del self._messages[key]
        for key, queue in list(self._duplicates.items()):
            self._prune(queue, duplicate_cutoff)
            if not queue:
                del self._duplicates[key]


def parse_duration(value: str) -> int:
    normalized = value.strip().casefold()
    match = re.fullmatch(r"(\d+)(s|m|h|d|秒|分|分钟|小时|天)", normalized)
    if not match:
        raise ValueError("时长格式应为 30s、10m、2h、1d 或对应中文单位")
    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = {
        "s": 1,
        "秒": 1,
        "m": 60,
        "分": 60,
        "分钟": 60,
        "h": 3600,
        "小时": 3600,
        "d": 86400,
        "天": 86400,
    }[unit]
    seconds = amount * multiplier
    if seconds < 60 or seconds > 366 * 24 * 60 * 60:
        raise ValueError("时长必须在 1 分钟到 366 天之间")
    return seconds
