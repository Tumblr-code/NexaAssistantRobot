from pathlib import Path


def test_release_probe_precedes_quiesced_backup_and_switch() -> None:
    script = (Path(__file__).parents[1] / "deploy" / "release.sh").read_text(encoding="utf-8")

    probe = script.index("probing GPT-5.6 capabilities")
    stop = script.index("for a final consistent database backup")
    backup = script.index("creating validated database backup")
    switch = script.index("switching current to")

    assert probe < stop < backup < switch
    assert 'systemctl start "${SERVICE_NAME}"' in script

    switch_function = script[
        script.index("switch_current_to()") : script.index("restore_previous_release()")
    ]
    assert switch_function.index("switched=1") < switch_function.index("mv -Tf")
