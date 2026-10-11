import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def _release_script() -> str:
    return (Path(__file__).parents[1] / "deploy" / "release.sh").read_text(encoding="utf-8")


def _embedded_python(script: str, anchor: str) -> str:
    anchor_index = script.index(anchor)
    start = script.index("<<'PY'", anchor_index) + len("<<'PY'")
    end = script.index("\nPY\n", start)
    return script[start:end].lstrip("\n")


def _run_embedded(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - sys.executable and a repository-owned snippet
        [sys.executable, "-", *arguments],
        input=script,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_release_arguments(*arguments: str) -> subprocess.CompletedProcess[str]:
    release_script = Path(__file__).parents[1] / "deploy" / "release.sh"
    return subprocess.run(  # noqa: S603 - repository-owned release script
        [str(release_script), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_probe_precedes_quiesced_backup_and_switch() -> None:
    script = _release_script()

    assert 'readonly CODE_SCHEMA_VERSION="9"' in script
    assert "raffle" not in script.casefold()
    assert "readonly AI_MODEL_PATTERN=" in script
    assert "deepseek" not in script.casefold()
    assert 'readonly APP_ROOT="/opt/projects/nexa-guardian/runtime"' in script
    assert "--allow-model-cooldown requires --ai-model" in script
    probe = script.index("probing the configured AI gateway and production settings")
    pause_timer = script.index("pausing ${BACKUP_TIMER_NAME}")
    backup_inactive = script.index("database backup service did not become idle")
    stop = script.index("for a final consistent database backup")
    backup = script.index("creating validated database backup")
    session_backup = script.index("creating validated Telegram session backup")
    switch = script.index("switching current to")
    database_validation = script.index("production schema version mismatch")
    verified_backup = script.index("running one verified database and Telegram session backup")
    resume_timer = script.rindex('systemctl start "${BACKUP_TIMER_NAME}"')

    assert (
        probe
        < pause_timer
        < backup_inactive
        < stop
        < backup
        < session_backup
        < switch
        < database_validation
        < verified_backup
        < resume_timer
    )
    assert 'systemctl start "${SERVICE_NAME}"' in script
    assert "backup_timer_paused=1" in script

    build_candidate = script.index('mv -T -- "${stage_path}" "${final_release}"')
    create_venv = script.index('"${PYTHON_BIN}" -m venv "${final_release}/.venv"')
    run_probe = script.index('"${final_release}/.venv/bin/nexa-guardian-probe"')
    assert build_candidate < create_venv < run_probe < pause_timer
    assert '"${stage_path}/.venv' not in script

    pause_block = script[script.index('log "pausing ${BACKUP_TIMER_NAME}') : backup_inactive]
    assert pause_block.index("backup_timer_paused=1") < pause_block.index(
        'systemctl stop "${BACKUP_TIMER_NAME}"'
    )
    assert "wait_for_backup_service_idle" in pause_block
    assert 'readonly TELEGRAM_SESSION_PATH="/var/lib/nexa-guardian/mtproto/admin.session"' in script
    assert "Telegram session permissions must not be wider than 0600" in script
    assert "PRAGMA quick_check" in script[session_backup:switch]
    assert "chmod(backup_path, 0o600)" in script[session_backup:switch]
    assert 'sha256sum -- "${session_backup_path}"' in script[session_backup:switch]
    assert "new backup did not create a matching database/session pair" in script
    assert "backup permissions are not 0600" in script

    stop_block = script[script.index('log "stopping ${SERVICE_NAME}') : backup]
    assert stop_block.index("service_stopped=1") < stop_block.index(
        'systemctl stop "${SERVICE_NAME}"'
    )

    resume_block = script[resume_timer : script.index('log "release is healthy')]
    assert resume_block.index('systemctl is-enabled --quiet "${BACKUP_TIMER_NAME}"') < (
        resume_block.index("backup_timer_paused=0")
    )
    assert resume_block.index('systemctl is-active --quiet "${BACKUP_TIMER_NAME}"') < (
        resume_block.index("backup_timer_paused=0")
    )

    assert 'safe_remove_tree "${candidate_real}" "${RELEASES_ROOT}"' in script
    assert 'is_same_or_below "${current_real}" "${candidate_real}"' in script
    assert "findmnt -rn -o TARGET" in script
    assert 'dirname -- "${previous_target}"' in script

    cleanup = script[script.index("cleanup()") : script.index("trap cleanup EXIT")]
    assert cleanup.index("local exit_code=$?") < cleanup.index("set +e")
    assert cleanup.index("local exit_code=$?") < cleanup.index("trap '' INT TERM")
    assert cleanup.index("trap '' INT TERM") < cleanup.index("set +e")
    assert cleanup.index('systemctl stop "${BACKUP_TIMER_NAME}"') < cleanup.index(
        "restore_previous_release"
    )
    assert cleanup.index("set +e") < cleanup.index("restore_previous_release")
    assert "backup timer remains stopped after incomplete rollback" in cleanup
    assert cleanup.count("(( release_stable == 1 )) || exit_code=1") == 5

    stable_tail = script[script.index("release_stable=1") :]
    assert " || die " not in stable_tail
    assert resume_block.index("release_stable=1") < resume_block.index("backup_timer_paused=0")

    switch_function = script[
        script.index("switch_current_to()") : script.index("restore_previous_release()")
    ]
    assert switch_function.index("switched=1") < switch_function.index("mv -Tf")

    config_backup = script.index('mv -- "${config_backup_staging}" "${config_backup_path}"')
    candidate_config = script.index('mktemp "${MANUAL_BACKUP_ROOT}/.candidate-env-')
    production_model_update = script.index('log "atomically applying AI_MODEL=')
    assert config_backup < candidate_config < probe < stop < production_model_update < switch
    assert 'candidate_config_path=""' not in script[probe:production_model_update]
    assert '--env-file "${probe_config_path}"' in script
    assert "probe_arguments+=(--allow-model-cooldown)" in script
    assert "operator-authorized exact 429 model_cooldown" in script
    assert 'chmod 0600 -- "${candidate_config_path}"' in script
    assert "configuration must contain exactly one literal {key.decode()}= line" in script
    assert "os.replace(staging_path, config_path)" in script
    assert "restored the original configuration before rollback" in script
    assert "restore_candidate_config_after_failed_rollback" in script

    rollback = script[
        script.index("restore_previous_release()") : script.index("restore_original_config()")
    ]
    assert rollback.index('systemctl stop "${SERVICE_NAME}"') < rollback.index(
        "restore_original_config"
    )
    assert rollback.index("restore_original_config") < rollback.index(
        'ln -s -- "${previous_target}"'
    )
    rollback_failure = rollback[rollback.index("failed to restore previous current link") - 1200 :]
    assert "restore_candidate_config_after_failed_rollback" in rollback_failure


def test_pre_switch_rollback_noop_succeeds_inside_failed_exit_trap() -> None:
    script = _release_script()
    function = script[
        script.index("restore_previous_release()") : script.index("restore_original_config()")
    ]
    bash = shutil.which("bash")
    assert bash is not None
    harness = (
        "set -Eeuo pipefail\nCURRENT_LINK=/unused\nswitched=0\nrelease_stable=0\n"
        + function
        + "\ncleanup() { set +e; if restore_previous_release; then "
        + "printf 'safe-to-restore-config-and-restart'; else printf 'recovery-skipped'; fi; }\n"
        + "trap cleanup EXIT\nexit 1\n"
    )
    result = subprocess.run(  # noqa: S603 - repository-owned function and fixed harness
        [bash], input=harness, text=True, capture_output=True, check=False
    )
    assert result.returncode == 1
    assert result.stdout == "safe-to-restore-config-and-restart"


def test_release_cooldown_exception_arguments_fail_closed(tmp_path: Path) -> None:
    base = (
        "--wheel",
        str(tmp_path / "nexa-guardian-test.whl"),
        "--git-sha",
        "0" * 40,
        "--schema-version",
        "9",
    )

    missing_model = _run_release_arguments(*base, "--allow-model-cooldown")
    wrong_model = _run_release_arguments(
        *base,
        "--ai-model",
        "not a model!",
        "--allow-model-cooldown",
    )
    duplicate = _run_release_arguments(
        *base,
        "--ai-model",
        "deepseek-flash",
        "--allow-model-cooldown",
        "--allow-model-cooldown",
    )

    assert missing_model.returncode != 0
    assert "requires --ai-model" in missing_model.stderr
    assert wrong_model.returncode != 0
    assert "not a valid model identifier" in wrong_model.stderr
    assert duplicate.returncode != 0
    assert "may only be provided once" in duplicate.stderr


def test_release_metadata_records_gateway_probe_policy(tmp_path: Path) -> None:
    script = _release_script()
    snippet = _embedded_python(script, '"${stage_path}/release.json"')

    for allowed, expected in (("0", "strict"), ("1", "allow_model_cooldown")):
        metadata_path = tmp_path / f"release-{allowed}.json"
        result = _run_embedded(
            snippet,
            str(metadata_path),
            "0" * 40,
            "2026-09-01T00:00:00Z",
            "1" * 64,
            "8",
            "nexa_guardian-0.6.7-py3-none-any.whl",
            allowed,
        )

        assert result.returncode == 0, result.stderr
        assert f'"gateway_probe_policy": "{expected}"' in metadata_path.read_text(encoding="utf-8")


def test_release_ai_model_update_is_atomic_and_preserves_other_config(tmp_path: Path) -> None:
    script = _release_script()
    snippet = _embedded_python(script, "update_ai_model_config()")
    config = tmp_path / "guardian.env"
    original = (
        b"SECRET=unchanged\nAI_MODEL=gpt-5.6-luna\nOTHER=value\n"
        b"AI_REASONING_EFFORT=high\nAI_NORMAL_REASONING_EFFORT=low\n"
        b"AI_SUSPICIOUS_REASONING_EFFORT=high\n"
    )
    config.write_bytes(original)
    config.chmod(0o600)

    result = _run_embedded(snippet, str(config), "deepseek-flash", "101")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "changed"
    assert config.read_bytes() == original.replace(b"gpt-5.6-luna", b"deepseek-flash")
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".guardian.env.ai-model.*")) == []


def test_release_ai_model_update_rejects_missing_or_duplicate_config(tmp_path: Path) -> None:
    script = _release_script()
    snippet = _embedded_python(script, "update_ai_model_config()")
    invalid_configs = (
        b"OTHER=value\n",
        b"AI_MODEL=gpt-5.6-sol\nAI_MODEL=gpt-5.6\n",
        b"AI_MODEL=gpt-5.6-luna\nAI_REASONING_EFFORT=high\nAI_MODEL=gpt-5.6-luna\n",
    )
    for index, original in enumerate(invalid_configs):
        config = tmp_path / f"guardian-{index}.env"
        config.write_bytes(original)
        config.chmod(0o600)

        result = _run_embedded(snippet, str(config), "deepseek-flash", f"102{index}")

        assert result.returncode != 0
        assert config.read_bytes() == original
        assert list(tmp_path.glob(f".{config.name}.ai-model.*")) == []


def test_release_ai_model_update_reports_unchanged_without_rewrite(tmp_path: Path) -> None:
    script = _release_script()
    snippet = _embedded_python(script, "update_ai_model_config()")
    config = tmp_path / "guardian.env"
    original = (
        b"SECRET=unchanged\nAI_MODEL=deepseek-flash\n"
        b"AI_REASONING_EFFORT=none\nAI_NORMAL_REASONING_EFFORT=none\n"
        b"AI_SUSPICIOUS_REASONING_EFFORT=none\n"
    )
    config.write_bytes(original)
    config.chmod(0o600)
    original_inode = config.stat().st_ino

    result = _run_embedded(snippet, str(config), "deepseek-flash", "104")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unchanged"
    assert config.read_bytes() == original
    assert config.stat().st_ino == original_inode


def test_release_config_rollback_restores_exact_backup(tmp_path: Path) -> None:
    script = _release_script()
    snippet = _embedded_python(script, 'restore_result="$(')
    backup = tmp_path / "pre-release.env"
    config = tmp_path / "guardian.env"
    original = (
        b"SECRET=unchanged\nAI_MODEL=gpt-5.6-luna\n"
        b"AI_REASONING_EFFORT=high\nAI_NORMAL_REASONING_EFFORT=low\n"
        b"AI_SUSPICIOUS_REASONING_EFFORT=high\n"
    )
    backup.write_bytes(original)
    backup.chmod(0o600)
    config.write_bytes(
        original.replace(b"gpt-5.6-luna", b"deepseek-flash").replace(b"=high\n", b"=low\n")
    )
    config.chmod(0o600)

    result = _run_embedded(snippet, str(backup), str(config), "600", "103")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "restored"
    assert config.read_bytes() == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".guardian.env.rollback.*")) == []


def test_runtime_paths_include_persistent_mtproto_session_and_backup() -> None:
    root = Path(__file__).parents[1]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    backup_unit = (root / "deploy/systemd/nexa-guardian-backup.service").read_text(encoding="utf-8")

    assert "TELEGRAM_SESSION_PATH: /data/mtproto/admin" in compose
    assert "Environment=TELEGRAM_SESSION_PATH=/var/lib/nexa-guardian/mtproto/admin" in backup_unit
    assert "--session-path /var/lib/nexa-guardian/mtproto/admin" in backup_unit


def test_release_credentials_are_atomic_and_rollback_restores_old_provider(tmp_path: Path) -> None:
    script = _release_script()
    update = _embedded_python(script, "update_ai_model_config()")
    rollback = _embedded_python(script, 'restore_result="$(')
    original = (
        b"BOT_TOKEN=unchanged\nAI_MODEL=gpt-5.6-luna\n"
        b"AI_REASONING_EFFORT=high\nAI_NORMAL_REASONING_EFFORT=low\n"
        b"AI_SUSPICIOUS_REASONING_EFFORT=high\n"
        b"OPENAI_BASE_URL=https://old.example\nOPENAI_API_KEY=old-placeholder\n"
    )
    config = tmp_path / "guardian.env"
    backup = tmp_path / "backup.env"
    credentials = tmp_path / "credentials.env"
    for path in (config, backup):
        path.write_bytes(original)
        path.chmod(0o600)
    credentials.write_bytes(
        b"OPENAI_BASE_URL=https://api.deepseek.com\nOPENAI_API_KEY=new-placeholder\n"
    )
    credentials.chmod(0o600)
    result = _run_embedded(update, str(config), "deepseek-flash", "105", str(credentials))
    assert result.returncode == 0, result.stderr
    updated = config.read_bytes()
    assert b"AI_MODEL=deepseek-flash\n" in updated
    assert b"OPENAI_API_KEY=new-placeholder\n" in updated
    assert b"OPENAI_BASE_URL=https://api.deepseek.com\n" in updated
    assert b"BOT_TOKEN=unchanged\n" in updated
    assert b"AI_REASONING_EFFORT=high\n" in updated
    assert "placeholder" not in result.stdout + result.stderr
    restored = _run_embedded(rollback, str(backup), str(config), "600", "106")
    assert restored.returncode == 0, restored.stderr
    assert config.read_bytes() == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


@pytest.mark.parametrize("invalid", ["symlink", "permissions", "host", "duplicate", "extra"])
def test_release_unsafe_credentials_do_not_modify_config(tmp_path: Path, invalid: str) -> None:
    snippet = _embedded_python(_release_script(), "update_ai_model_config()")
    config = tmp_path / "guardian.env"
    config.write_bytes(b"SECRET=must-not-change\n")
    credentials = tmp_path / "credentials.env"
    content = b"OPENAI_BASE_URL=https://api.deepseek.com\nOPENAI_API_KEY=placeholder\n"
    if invalid == "host":
        content = content.replace(b"https://api.deepseek.com", b"http://api.deepseek.com")
    elif invalid == "duplicate":
        content += b"OPENAI_API_KEY=duplicate\n"
    elif invalid == "extra":
        content += b"BOT_TOKEN=not-allowed\n"
    credentials.write_bytes(content)
    credentials.chmod(0o644 if invalid == "permissions" else 0o600)
    if invalid == "symlink":
        link = tmp_path / "link.env"
        link.symlink_to(credentials)
        credentials = link
    result = _run_embedded(snippet, str(config), "deepseek-flash", "107", str(credentials))
    assert result.returncode != 0
    assert config.read_bytes() == b"SECRET=must-not-change\n"
    assert "placeholder" not in result.stdout + result.stderr
