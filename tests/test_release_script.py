from pathlib import Path


def test_release_probe_precedes_quiesced_backup_and_switch() -> None:
    script = (Path(__file__).parents[1] / "deploy" / "release.sh").read_text(encoding="utf-8")

    assert 'readonly CODE_SCHEMA_VERSION="7"' in script
    probe = script.index("probing GPT-5.6 Sol adaptive-reasoning capabilities")
    pause_timer = script.index("pausing ${BACKUP_TIMER_NAME}")
    backup_inactive = script.index("database backup service did not become idle")
    stop = script.index("for a final consistent database backup")
    backup = script.index("creating validated database backup")
    switch = script.index("switching current to")
    database_validation = script.index("production schema version mismatch")
    verified_backup = script.index("running one verified database backup")
    resume_timer = script.rindex('systemctl start "${BACKUP_TIMER_NAME}"')

    assert (
        probe
        < pause_timer
        < backup_inactive
        < stop
        < backup
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
    assert cleanup.count("(( release_stable == 1 )) || exit_code=1") == 4

    stable_tail = script[script.index("release_stable=1") :]
    assert " || die " not in stable_tail
    assert resume_block.index("release_stable=1") < resume_block.index("backup_timer_paused=0")

    switch_function = script[
        script.index("switch_current_to()") : script.index("restore_previous_release()")
    ]
    assert switch_function.index("switched=1") < switch_function.index("mv -Tf")
