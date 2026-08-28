import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nexa_guardian.backup import backup_database, prune_backups
from nexa_guardian.maintenance import scrub_reasons


def test_backup_database_is_consistent(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE example (value TEXT NOT NULL)")
        source.execute("INSERT INTO example VALUES ('kept')")
        source.commit()

    backup_path = backup_database(source_path, tmp_path / "backups")

    assert backup_path.exists()
    assert backup_path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT value FROM example").fetchone() == ("kept",)
        assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_backup_refuses_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "missing.db", tmp_path / "backups")

    assert not (tmp_path / "missing.db").exists()


def test_backup_retention_only_removes_expired_standard_files(tmp_path: Path) -> None:
    old = tmp_path / "guardian-20260601T000000Z.db"
    current = tmp_path / "guardian-20260820T000000Z.db"
    manual = tmp_path / "manual.db"
    nested = tmp_path / "manual" / "guardian-20260101T000000Z.db"
    nested.parent.mkdir()
    for path in (old, current, manual, nested):
        path.touch()

    removed = prune_backups(
        tmp_path,
        retention_days=30,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert removed == (old,)
    assert not old.exists()
    assert current.exists()
    assert manual.exists()
    assert nested.exists()


def test_scrub_reasons_clears_and_compacts_database(tmp_path: Path) -> None:
    database = tmp_path / "guardian.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE moderation_log (reason TEXT NOT NULL);
            CREATE TABLE warnings (last_reason TEXT NOT NULL);
            INSERT INTO moderation_log VALUES ('未填写原因');
            INSERT INTO warnings VALUES ('非白名单外链');
            """
        )

    result = scrub_reasons(database)

    assert result.moderation_rows == 1
    assert result.warning_rows == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT reason FROM moderation_log").fetchone() == ("",)
        assert connection.execute("SELECT last_reason FROM warnings").fetchone() == ("",)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    contents = database.read_bytes()
    assert "未填写原因".encode() not in contents
    assert "非白名单外链".encode() not in contents
