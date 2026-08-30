from __future__ import annotations

import argparse
import os
import re
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BACKUP_NAME_RE = re.compile(
    r"(?:guardian-(\d{8}T\d{6}Z)\.db|telegram-session-(\d{8}T\d{6}Z)\.session)"
)
UTC = timezone.utc
DEFAULT_PRODUCTION_SESSION_PATH = Path("/var/lib/nexa-guardian/mtproto/admin")


def resolve_telethon_session_file(session_path: Path) -> Path:
    if str(session_path).endswith(".session"):
        return session_path
    return Path(f"{session_path}.session")


def _validate_regular_file(source_path: Path, *, label: str, sensitive: bool) -> None:
    try:
        metadata = source_path.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label}不存在：{source_path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label}必须是普通的非符号链接文件：{source_path}")
    if sensitive and stat.S_IMODE(metadata.st_mode) & ~0o600:
        raise PermissionError(f"{label}权限不得宽于 0600：{source_path}")


def _prepare_target_directory(target_directory: Path) -> None:
    target_directory.mkdir(parents=True, exist_ok=True)
    metadata = target_directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"备份目录必须是普通目录：{target_directory}")


def _reserve_target(target_path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target_path, flags, 0o600)
    os.close(descriptor)


def _backup_sqlite(source_path: Path, target_path: Path) -> None:
    if target_path.exists() or target_path.is_symlink():
        raise FileExistsError(f"备份文件已存在：{target_path}")
    _reserve_target(target_path)

    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    try:
        with (
            sqlite3.connect(source_uri, uri=True) as source,
            sqlite3.connect(target_path) as target,
        ):
            source.backup(target)
            result = target.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError(f"备份数据库 quick_check 失败：{result}")
            foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise RuntimeError(f"备份数据库 foreign_key_check 失败：{foreign_keys}")
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    target_path.chmod(0o600)


def backup_database(
    source_path: Path,
    target_directory: Path,
    *,
    timestamp: str | None = None,
) -> Path:
    _validate_regular_file(source_path, label="源数据库", sensitive=False)
    _prepare_target_directory(target_directory)
    backup_timestamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target_path = target_directory / f"guardian-{backup_timestamp}.db"
    _backup_sqlite(source_path, target_path)
    return target_path


def backup_session_database(
    session_path: Path,
    target_directory: Path,
    *,
    timestamp: str | None = None,
) -> Path:
    source_path = resolve_telethon_session_file(session_path)
    _validate_regular_file(source_path, label="Telegram 会话数据库", sensitive=True)
    _prepare_target_directory(target_directory)
    backup_timestamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target_path = target_directory / f"telegram-session-{backup_timestamp}.session"
    _backup_sqlite(source_path, target_path)
    return target_path


def prune_backups(
    target_directory: Path, *, retention_days: int, now: datetime | None = None
) -> tuple[Path, ...]:
    if retention_days < 1:
        raise ValueError("retention_days 必须大于或等于 1")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    removed: list[Path] = []
    for path in target_directory.iterdir():
        if not path.is_file() or (match := BACKUP_NAME_RE.fullmatch(path.name)) is None:
            continue
        timestamp = match.group(1) or match.group(2)
        created_at = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if created_at < cutoff:
            path.unlink()
            removed.append(path)
    return tuple(removed)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="在线备份 Nexa Guardian SQLite 数据库")
    parser.add_argument(
        "target_directory",
        nargs="?",
        type=Path,
        default=Path("backups"),
        help="备份目录，默认 ./backups",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=30,
        help="自动备份保留天数，默认 30；只清理备份目录根部的标准命名文件",
    )
    configured_session = os.getenv("TELEGRAM_SESSION_PATH", "").strip()
    production_session = resolve_telethon_session_file(DEFAULT_PRODUCTION_SESSION_PATH)
    default_session_path = (
        Path(configured_session).expanduser()
        if configured_session
        else DEFAULT_PRODUCTION_SESSION_PATH
        if production_session.is_file()
        else None
    )
    parser.add_argument(
        "--session-path",
        type=Path,
        default=default_session_path,
        help="可选的 Telethon 会话路径；传入后与主数据库一同校验和备份",
    )
    args = parser.parse_args()
    source_path = Path(os.getenv("DATABASE_PATH", "data/guardian.db")).expanduser()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = backup_database(source_path, args.target_directory, timestamp=timestamp)
    if args.session_path is not None:
        try:
            backup_session_database(
                args.session_path,
                args.target_directory,
                timestamp=timestamp,
            )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    prune_backups(args.target_directory, retention_days=args.retention_days)
    print(path)


if __name__ == "__main__":
    main()
