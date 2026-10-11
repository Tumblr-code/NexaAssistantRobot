from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ScrubResult:
    path: Path
    moderation_rows: int
    warning_rows: int


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def scrub_reasons(path: Path) -> ScrubResult:
    if not path.is_file():
        raise FileNotFoundError(f"数据库不存在：{path}")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA secure_delete=ON")
        moderation_rows = 0
        warning_rows = 0
        if _table_exists(connection, "moderation_log"):
            cursor = connection.execute("UPDATE moderation_log SET reason = '' WHERE reason <> ''")
            moderation_rows = cursor.rowcount
        if _table_exists(connection, "warnings"):
            cursor = connection.execute(
                "UPDATE warnings SET last_reason = '' WHERE last_reason <> ''"
            )
            warning_rows = cursor.rowcount
        connection.commit()
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise RuntimeError(f"数据库 quick_check 失败：{path}: {quick_check}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"数据库 foreign_key_check 失败：{path}: {foreign_keys}")
    return ScrubResult(path, moderation_rows, warning_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="清除 Nexa Guardian 数据库中的原因字段")
    parser.add_argument("database", nargs="+", type=Path)
    args = parser.parse_args()
    for database in args.database:
        result = scrub_reasons(database)
        print(
            f"{result.path}: moderation_rows={result.moderation_rows} "
            f"warning_rows={result.warning_rows}"
        )


if __name__ == "__main__":
    main()
