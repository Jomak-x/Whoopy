from __future__ import annotations

import json

from pytest import CaptureFixture, MonkeyPatch

from whoopy.cli import main
from whoopy.hardware import HardwareSnapshot


def test_help_exits_successfully(capsys: CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "Local-first" in capsys.readouterr().out


def test_config_show_prints_resolved_settings(capsys: CaptureFixture[str]) -> None:
    assert main(["config", "show", "--tts-voice", "test_voice"]) == 0

    output = capsys.readouterr().out
    assert "voice: test_voice" in output


def test_doctor_prints_machine_readable_recommendation(
    capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    snapshot = HardwareSnapshot(
        operating_system="linux",
        architecture="x86_64",
        cpu_count=8,
        total_ram_gb=16,
        available_ram_gb=9,
        free_disk_gb=20,
        accelerators=["cpu"],
    )
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda: snapshot)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["supported"] is True
    assert output["selected_profile"]["name"] == "standard"
