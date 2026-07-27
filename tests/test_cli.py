from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pytest import CaptureFixture, MonkeyPatch

from whoopy.adapters.llm import LlamaCppScriptGenerator
from whoopy.adapters.tts import SherpaOnnxKokoroAdapter
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.cli import main
from whoopy.hardware import HardwareSnapshot
from whoopy.ports import AdapterMetadata, ScriptGenerationRequest, ScriptGenerationResult


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


def test_run_and_worker_commands_write_the_phase_three_artifacts(
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
    assert completed["schema_version"] == 3
    assert completed["recovery"]["speech_segments_completed"] == 2

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

    assert (
        main(
            [
                "cache",
                "stats",
                "--runs-dir",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    cache_stats = json.loads(capsys.readouterr().out)
    assert cache_stats["entries"] == 2
    assert cache_stats["valid_entries"] == 2
    assert cache_stats["corrupt_entries"] == 0


def test_models_doctor_resolves_a_plan_without_loading_models(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
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
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: snapshot)

    assert (
        main(
            [
                "models",
                "doctor",
                "--profile",
                "standard",
                "--models-dir",
                str(tmp_path),
                "--json",
            ]
        )
        == 1
    )

    output = json.loads(capsys.readouterr().out)
    assert output["hardware"]["selected_profile"]["name"] == "standard"
    assert len(output["artifacts"]) == 5
    assert {artifact["state"] for artifact in output["artifacts"]} == {"missing"}
    assert output["ready"] is False
    assert output["loaded_models"] is False


def test_models_install_can_prepare_a_profile_without_network(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    payload = b"tiny offline artifact"
    digest = hashlib.sha256(payload).hexdigest()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "runtime_profiles.yaml").write_text(
        """
standard:
  rank: 2
  min_total_ram_gb: 16
  min_available_ram_gb: 8
  min_free_disk_gb: 12
  approximate_download_gb: 0.001
  llm_runtime: fixture
  llm_model_class: fixture
  tts_runtime: fixture
  modes: [local_llm]
""".lstrip(),
        encoding="utf-8",
    )
    lock_path = config_dir / "artifacts.yaml"
    lock_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "artifacts": [
                    {
                        "artifact_id": "tiny_model",
                        "component": "llm_model",
                        "display_name": "Tiny model",
                        "version": "test-v1",
                        "kind": "model",
                        "license_id": "Apache-2.0",
                        "source_url": "https://example.invalid/tiny.bin",
                        "filename": "tiny.bin",
                        "size_bytes": len(payload),
                        "sha256": digest,
                        "operating_systems": ["linux"],
                        "architectures": ["x86_64"],
                    }
                ],
                "profiles": {"standard": {"components": ["llm_model"]}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    offline_dir = tmp_path / "offline"
    offline_dir.mkdir()
    (offline_dir / "tiny.bin").write_bytes(payload)
    models_dir = tmp_path / "managed"
    snapshot = HardwareSnapshot(
        operating_system="linux",
        architecture="x86_64",
        cpu_count=8,
        total_ram_gb=16,
        available_ram_gb=9,
        free_disk_gb=20,
        accelerators=["cpu"],
    )
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: snapshot)

    arguments = [
        "models",
        "install",
        "--profile",
        "standard",
        "--config-dir",
        str(config_dir),
        "--models-dir",
        str(models_dir),
        "--offline-dir",
        str(offline_dir),
        "--no-network",
        "--json",
    ]
    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["installed"] == ["tiny_model"]
    assert first["reused"] == []

    assert main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["installed"] == []
    assert second["reused"] == ["tiny_model"]


def test_generate_script_file_writes_real_flow_artifacts_with_adapter_contract(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    snapshot = HardwareSnapshot(
        operating_system="linux",
        architecture="x86_64",
        cpu_count=8,
        total_ram_gb=8,
        available_ram_gb=4,
        free_disk_gb=20,
        accelerators=["cpu"],
    )
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: snapshot)
    monkeypatch.setattr(
        SherpaOnnxKokoroAdapter,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: FixtureSpeechSynthesizer()),
    )
    script_path = tmp_path / "meditation.md"
    script_path.write_text(
        "# Test\n\nWelcome to this moment.\n\n[pause: 1.5s]\n\nLet your shoulders soften.",
        encoding="utf-8",
    )
    runs_dir = tmp_path / "runs"

    assert (
        main(
            [
                "generate",
                "--script-file",
                str(script_path),
                "--runs-dir",
                str(runs_dir),
                "--models-dir",
                str(tmp_path / "models"),
                "--json",
            ]
        )
        == 0
    )

    completed = json.loads(capsys.readouterr().out)
    run_directory = runs_dir / completed["run_id"]
    assert completed["schema_version"] == 4
    assert completed["status"] == "completed"
    assert completed["source_kind"] == "script_file"
    assert (run_directory / "script.md").read_text(encoding="utf-8").startswith("# Test")
    assert (run_directory / "resolved-config.json").is_file()
    assert (run_directory / "model-metadata.json").is_file()
    assert (run_directory / "timeline.json").is_file()
    assert (run_directory / "narration.wav").is_file()
    assert (run_directory / "audio-manifest.json").is_file()
    quality = json.loads((run_directory / "quality.json").read_text(encoding="utf-8"))
    assert quality["passed"] is True


class _DraftFixture:
    metadata = AdapterMetadata(
        adapter_id="test.draft",
        versioned_model_id="fixture@1",
        runtime_id="python",
        runtime_version="test",
        license_id="CC0-1.0",
        device="fixture",
    )

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        if "Create the structural JSON plan" in request.prompt:
            value = {
                "title": "Test Meditation",
                "intention": "Offer a quiet pause.",
                "sections": [
                    {
                        "id": section_id,
                        "title": section_id.title(),
                        "purpose": f"Guide the {section_id} section.",
                        "weight": 1,
                        "pause_seconds": 6,
                    }
                    for section_id in ("arrive", "notice", "return")
                ],
            }
        else:
            section_id = request.prompt.split("Section ID: ", 1)[1].splitlines()[0]
            value = {
                "section_id": section_id,
                "text": (
                    f"{section_id.title()} gently into this quiet moment. "
                    "Notice the steady support beneath you. "
                    "Let this simple experience be enough for now."
                ),
            }
        return ScriptGenerationResult(
            text=json.dumps(value),
            metadata=self.metadata,
            elapsed_seconds=0.01,
        )


def test_draft_command_persists_validated_local_generation(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
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
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: snapshot)
    monkeypatch.setattr(
        LlamaCppScriptGenerator,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: _DraftFixture()),
    )
    output_dir = tmp_path / "drafts"

    assert (
        main(
            [
                "draft",
                "A gentle grounding pause.",
                "--minutes",
                "1",
                "--profile",
                "standard",
                "--models-dir",
                str(tmp_path / "models"),
                "--output-dir",
                str(output_dir),
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    draft = output_dir / output["draft_id"]
    assert output["sections"] == 3
    assert (draft / "request.json").is_file()
    assert (draft / "plan.json").is_file()
    assert (draft / "script.md").is_file()
    assert (draft / "timeline.json").is_file()
    assert len(list((draft / "sections").glob("*.json"))) == 3


def test_generate_prompt_joins_validated_draft_to_real_audio_contract(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
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
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: snapshot)
    monkeypatch.setattr(
        LlamaCppScriptGenerator,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: _DraftFixture()),
    )
    monkeypatch.setattr(
        SherpaOnnxKokoroAdapter,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: FixtureSpeechSynthesizer()),
    )
    runs_dir = tmp_path / "runs"

    assert (
        main(
            [
                "generate",
                "A gentle grounding pause.",
                "--minutes",
                "1",
                "--profile",
                "standard",
                "--models-dir",
                str(tmp_path / "models"),
                "--runs-dir",
                str(runs_dir),
                "--json",
            ]
        )
        == 0
    )

    completed = json.loads(capsys.readouterr().out)
    run = runs_dir / completed["run_id"]
    timeline = json.loads((run / "timeline.json").read_text(encoding="utf-8"))
    metadata = json.loads((run / "model-metadata.json").read_text(encoding="utf-8"))
    assert completed["schema_version"] == 5
    assert completed["source_kind"] == "generated_prompt"
    assert completed["status"] == "completed"
    assert timeline["source"] == "generated_prompt"
    assert metadata["llm"]["adapter_id"] == "test.draft"
    assert (run / "plan.json").is_file()
    assert len(list((run / "draft-sections").glob("*.json"))) == 3
    assert len(list((run / "raw-model-output").glob("*.json"))) == 4
    assert (run / "narration.wav").is_file()
    assert json.loads((run / "quality.json").read_text(encoding="utf-8"))["passed"] is True
