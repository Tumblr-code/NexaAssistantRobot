from __future__ import annotations

import argparse
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BACKUP_NAME_RE = re.compile(r"guardian-(\d{8}T\d{6}Z)\.db")
UTC = timezone.utc


def backup_database(source_path: Path, target_directory: Path) -> Path:
    if not source_path.is_file():
        raise FileNotFoundError(f"源数据库不存在：{source_path}")
    target_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target_path = target_directory / f"guardian-{timestamp}.db"
    if target_path.exists():
        raise FileExistsError(f"备份文件已存在：{target_path}")

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
        created_at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if created_at < cutoff:
            path.unlink()
            removed.append(path)
    return tuple(removed)


def main() -> None:
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
    args = parser.parse_args()
    load_dotenv()
    source_path = Path(os.getenv("DATABASE_PATH", "data/guardian.db")).expanduser()
    path = backup_database(source_path, args.target_directory)
    prune_backups(args.target_directory, retention_days=args.retention_days)
    print(path)


if __name__ == "__main__":
    main()
