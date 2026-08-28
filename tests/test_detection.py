from aiogram.enums import MessageEntityType
from aiogram.types import MessageEntity

from nexa_guardian.detection import (
    AdDetector,
    DetectionInput,
    FloodGuard,
    domain_from_url,
    extract_entity_urls,
    human_duration,
    is_allowed_domain,
    normalize_domain,
    parse_duration,
)


def detector(**overrides: bool) -> AdDetector:
    values = {
        "block_external_links": True,
        "block_forwards": False,
        "block_channel_mentions": False,
    }
    values.update(overrides)
    return AdDetector(**values)


def test_external_link_is_blocked() -> None:
    result = detector().detect(
        DetectionInput(text="低价推广 https://spam.example/buy"),
        allowed_domains={"nexastore.cn"},
        blocked_words=set(),
    )
    assert result is not None
    assert result.code == "external_link"
    assert "spam.example" in result.detail


def test_allowed_domain_and_subdomain_are_kept() -> None:
    allowed = {"nexastore.cn"}
    root = detector().detect(
        DetectionInput(text="https://nexastore.cn/a"),
        allowed_domains=allowed,
        blocked_words=set(),
    )
    child = detector().detect(
        DetectionInput(text="https://openai.nexastore.cn/a"),
        allowed_domains=allowed,
        blocked_words=set(),
    )
    lookalike = detector().detect(
        DetectionInput(text="https://nexastore.cn.evil.example/a"),
        allowed_domains=allowed,
        blocked_words=set(),
    )
    assert root is None
    assert child is None
    assert lookalike is not None


def test_hidden_text_link_is_checked() -> None:
    result = detector().detect(
        DetectionInput(text="点这里", entity_urls=("https://evil.example",)),
        allowed_domains=set(),
        blocked_words=set(),
    )
    assert result is not None
    assert result.code == "external_link"


def test_utf16_entity_extraction_handles_emoji() -> None:
    text = "😀 https://evil.example"
    link = "https://evil.example"
    entity = MessageEntity(
        type=MessageEntityType.URL,
        offset=3,
        length=len(link),
    )
    assert extract_entity_urls(text, [entity]) == (link,)


def test_blocked_word_matches_simple_evasion() -> None:
    result = detector(block_external_links=False).detect(
        DetectionInput(text="加 微\u200b信，低价代充"),
        allowed_domains=set(),
        blocked_words={"加微信"},
    )
    assert result is not None
    assert result.code == "blocked_word"


def test_optional_forward_and_mention_rules() -> None:
    forward = detector(block_forwards=True).detect(
        DetectionInput(text="普通转发", is_forwarded=True),
        allowed_domains=set(),
        blocked_words=set(),
    )
    mention = detector(block_channel_mentions=True).detect(
        DetectionInput(text="加我进群 @spam_channel"),
        allowed_domains=set(),
        blocked_words=set(),
    )
    assert forward is not None and forward.code == "forward"
    assert mention is not None and mention.code == "mention"


def test_normal_member_mention_is_not_treated_as_advertising() -> None:
    result = detector(block_channel_mentions=True).detect(
        DetectionInput(text="@helpful_user 你能看一下这个问题吗"),
        allowed_domains=set(),
        blocked_words=set(),
    )
    assert result is None


def test_multiple_accounts_are_treated_as_account_promotion() -> None:
    result = detector(block_channel_mentions=True).detect(
        DetectionInput(text="@spam_one @spam_two"),
        allowed_domains=set(),
        blocked_words=set(),
    )
    assert result is not None and result.code == "mention"


def test_fullwidth_link_is_normalized_before_detection() -> None:
    result = detector().detect(
        DetectionInput(text="ＨＴＴＰＳ：／／spam.example/buy"),
        allowed_domains=set(),
        blocked_words=set(),
    )
    assert result is not None and result.code == "external_link"


def test_domain_helpers() -> None:
    assert normalize_domain("HTTPS://OpenAI.NexaStore.CN/path") == "openai.nexastore.cn"
    assert domain_from_url("www.Example.COM/path,") == "www.example.com"
    assert is_allowed_domain("a.example.com", {"example.com"})
    assert not is_allowed_domain("badexample.com", {"example.com"})


def test_flood_thresholds() -> None:
    guard = FloodGuard(
        window_seconds=8,
        max_messages=2,
        duplicate_window_seconds=30,
        duplicate_max_messages=2,
    )
    assert guard.check(-100, 1, "first", now=0) is None
    assert guard.check(-100, 1, "second", now=1) is None
    result = guard.check(-100, 1, "third", now=2)
    assert result is not None and result.code == "flood"


def test_duplicate_thresholds() -> None:
    guard = FloodGuard(
        window_seconds=1,
        max_messages=100,
        duplicate_window_seconds=30,
        duplicate_max_messages=2,
    )
    assert guard.check(-100, 2, "same content", now=0) is None
    assert guard.check(-100, 2, "same content", now=2) is None
    result = guard.check(-100, 2, "same content", now=4)
    assert result is not None and result.code == "duplicate"


def test_duration_parser_and_display() -> None:
    assert parse_duration("60s") == 60
    assert parse_duration("10分钟") == 600
    assert parse_duration("2h") == 7200
    assert parse_duration("1天") == 86400
    assert human_duration(7200) == "2 小时"


def test_duration_rejects_telegram_forever_boundaries() -> None:
    for value in ("59s", "367d", "ten minutes"):
        try:
            parse_duration(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid duration: {value}")
