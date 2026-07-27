from __future__ import annotations

import json
from pathlib import Path

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


def test_run_and_worker_commands_write_the_phase_two_artifacts(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "run",
                "create",
                "A calm one-minute pause.",
                "--runs-dir",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    queued = json.loads(capsys.readouterr().out)
    assert queued["status"] == "queued"

    assert (
        main(
            [
                "worker",
                "process",
                queued["run_id"],
                "--runs-dir",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed"
    assert completed["timeline_artifact"] == "timeline.json"
    assert completed["audio_artifact"] == "narration.wav"
    assert completed["audio_manifest_artifact"] == "audio-manifest.json"
    assert completed["quality_artifact"] == "quality.json"

    run_directory = tmp_path / queued["run_id"]
    assert (run_directory / "run.json").is_file()
    assert (run_directory / "narration.wav").is_file()
    assert (run_directory / "audio-manifest.json").is_file()
    assert (run_directory / "quality.json").is_file()
    timeline = json.loads((run_directory / "timeline.json").read_text(encoding="utf-8"))
    assert [segment["type"] for segment in timeline["segments"]] == [
        "SPEECH",
        "SILENCE",
        "SPEECH",
    ]
    quality = json.loads((run_directory / "quality.json").read_text(encoding="utf-8"))
    assert quality["passed"] is True
