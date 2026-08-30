import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import nexa_guardian.backup as backup_module
from nexa_guardian.backup import (
    backup_database,
    backup_session_database,
    prune_backups,
    resolve_telethon_session_file,
)
from nexa_guardian.maintenance import scrub_reasons

UTC = timezone.utc


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


def test_backup_session_database_is_consistent_and_private(tmp_path: Path) -> None:
    session_path = tmp_path / "mtproto" / "admin"
    session_file = resolve_telethon_session_file(session_path)
    session_file.parent.mkdir()
    with sqlite3.connect(session_file) as session:
        session.execute("CREATE TABLE version (version INTEGER NOT NULL)")
        session.execute("INSERT INTO version VALUES (1)")
        session.commit()
    session_file.chmod(0o600)

    backup_path = backup_session_database(
        session_path,
        tmp_path / "backups",
        timestamp="20260827T000000Z",
    )

    assert backup_path.name == "telegram-session-20260827T000000Z.session"
    assert backup_path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT version FROM version").fetchone() == (1,)
        assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_backup_session_rejects_symlink_and_broad_permissions(tmp_path: Path) -> None:
    real_session = tmp_path / "real.session"
    with sqlite3.connect(real_session) as connection:
        connection.execute("CREATE TABLE example (value INTEGER)")
    real_session.chmod(0o600)
    symlink = tmp_path / "linked.session"
    symlink.symlink_to(real_session)

    with pytest.raises(ValueError, match="非符号链接"):
        backup_session_database(symlink, tmp_path / "backups")

    real_session.chmod(0o640)
    with pytest.raises(PermissionError, match="不得宽于 0600"):
        backup_session_database(real_session, tmp_path / "backups")


def test_backup_cli_removes_new_database_backup_when_session_backup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "guardian.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE example (value INTEGER)")
    backups = tmp_path / "backups"
    monkeypatch.setenv("DATABASE_PATH", str(source))
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", str(tmp_path / "admin"))
    monkeypatch.setattr(sys, "argv", ["nexa-guardian-backup", str(backups)])

    def fail_session(*_args: object, **_kwargs: object) -> Path:
        raise RuntimeError("session backup failed")

    monkeypatch.setattr(backup_module, "backup_session_database", fail_session)

    with pytest.raises(RuntimeError, match="session backup failed"):
        backup_module.main()

    assert list(backups.glob("guardian-*.db")) == []


def test_backup_rejects_symlink_target_directory(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE example (value INTEGER)")
    real_directory = tmp_path / "real-backups"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked-backups"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="备份目录必须是普通目录"):
        backup_database(source_path, linked_directory)


def test_backup_retention_only_removes_expired_standard_files(tmp_path: Path) -> None:
    old = tmp_path / "guardian-20260601T000000Z.db"
    current = tmp_path / "guardian-20260820T000000Z.db"
    old_session = tmp_path / "telegram-session-20260601T000000Z.session"
    current_session = tmp_path / "telegram-session-20260820T000000Z.session"
    manual = tmp_path / "manual.db"
    nested = tmp_path / "manual" / "guardian-20260101T000000Z.db"
    nested.parent.mkdir()
    for path in (old, current, old_session, current_session, manual, nested):
        path.touch()

    removed = prune_backups(
        tmp_path,
        retention_days=30,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )

    assert removed == (old, old_session)
    assert not old.exists()
    assert not old_session.exists()
    assert current.exists()
    assert current_session.exists()
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
