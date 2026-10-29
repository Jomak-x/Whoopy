from __future__ import annotations

import hashlib
import json
import shutil
import signal
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pytest import CaptureFixture, MonkeyPatch

from whoopy.adapters.llm import LlamaCppScriptGenerator
from whoopy.adapters.tts import SherpaOnnxKokoroAdapter
from whoopy.adapters.tts.mlx_qwen3_tts import MLX_QWEN_BACKENDS
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.cli import (
    QWEN_BACKEND_VARIANTS,
    _apply_global_speech_overrides,
    _apply_speech_capability_fallbacks,
    main,
)
from whoopy.hardware import HardwareSnapshot
from whoopy.pipeline.generation import PendingGenerationConfig, TTSRunSettings
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.runs import RunExecution, RunStage, RunStatus, RunStore
from whoopy.ports import (
    AdapterMetadata,
    ScriptGenerationRequest,
    ScriptGenerationResult,
    SpeechCapabilities,
)
from whoopy.timeline import PronunciationOverride, SpeechSegment, Timeline

RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _standard_snapshot() -> HardwareSnapshot:
    return HardwareSnapshot(
        operating_system="linux",
        architecture="x86_64",
        cpu_count=8,
        total_ram_gb=16,
        available_ram_gb=9,
        free_disk_gb=20,
        accelerators=["cpu"],
    )


class _ClosableFixture(FixtureSpeechSynthesizer):
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def test_speech_capability_fallbacks_persist_only_effective_controls() -> None:
    timeline = Timeline(
        schema_version=3,
        run_id=RUN_ID,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        source="script_file",
        segments=[
            SpeechSegment(
                id="speech-0001",
                text="Settle in.",
                delivery="calm",
                pace="slow",
                language="French",
                target_duration_ms=4_000,
                pronunciation_overrides=(
                    PronunciationOverride(source="Whoopy", pronunciation="Whoo-pee"),
                ),
            )
        ],
    )

    effective = _apply_speech_capability_fallbacks(
        timeline,
        SpeechCapabilities(voice_modes=("reference",)),
        voice_mode="reference",
    )

    speech = effective.segments[0]
    assert isinstance(speech, SpeechSegment)
    assert speech.voice_mode == "reference"
    assert speech.delivery == "neutral"
    assert speech.intensity == "subtle"
    assert speech.pace == "natural"
    assert speech.language == "English"
    assert speech.pronunciation_overrides == ()
    assert speech.target_duration_ms is None


def test_help_exits_successfully(capsys: CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "Local-first" in capsys.readouterr().out


def test_models_runtime_list_reports_unpublished_packs_truthfully(
    tmp_path: Path, capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr("whoopy.model_packs.runtime.current_runtime_target", lambda: "darwin-arm64")

    assert (
        main(
            [
                "models",
                "runtime",
                "list",
                "--runtime-registry",
                "config/runtime_packs.yaml",
                "--models-dir",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert {item["runtime_id"] for item in output["runtime_packs"]} == {
        "fish-speech",
        "moss-tts",
        "qwen3-tts",
    }
    states = {item["runtime_id"]: item["state"] for item in output["runtime_packs"]}
    assert states == {
        "fish-speech": "missing",
        "moss-tts": "missing",
        "qwen3-tts": "missing",
    }
    assert all(item["remedy"] for item in output["runtime_packs"])


def test_models_runtime_install_refuses_unpinned_release(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "models",
                "runtime",
                "install",
                "qwen3-tts",
                "--runtime-registry",
                "config/runtime_packs.yaml",
                "--models-dir",
                str(tmp_path),
                "--no-network",
                "--json",
            ]
        )


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


def test_model_pack_commands_use_the_managed_pack_boundary(
    capsys: CaptureFixture[str], monkeypatch: MonkeyPatch
) -> None:
    calls: list[tuple[str, object]] = []

    class FakePacks:
        def list(self) -> dict[str, object]:
            calls.append(("list", None))
            return {"packs": []}

        def remove(self, pack_id: str, *, confirmed: bool) -> dict[str, object]:
            calls.append(("remove", (pack_id, confirmed)))
            return {"receipt_id": "trash-1", "pack_id": pack_id}

    monkeypatch.setattr("whoopy.cli._model_pack_service", lambda _args: FakePacks())

    assert main(["models", "pack", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"packs": []}
    assert calls == [("list", None)]

    assert main(["models", "pack", "remove", "moss-local-5b", "--confirm", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "receipt_id": "trash-1",
        "pack_id": "moss-local-5b",
    }
    assert calls[-1] == ("remove", ("moss-local-5b", True))


def test_model_pack_remove_requires_explicit_confirmation() -> None:
    with pytest.raises(SystemExit, match="2"):
        main(["models", "pack", "remove", "moss-local-5b"])


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
    synthesizers: list[_ClosableFixture] = []

    def synthesizer_factory(**_kwargs: object) -> _ClosableFixture:
        synthesizer = _ClosableFixture()
        synthesizers.append(synthesizer)
        return synthesizer

    monkeypatch.setattr(
        SherpaOnnxKokoroAdapter,
        "from_artifact_store",
        classmethod(lambda _cls, **kwargs: synthesizer_factory(**kwargs)),
    )
    script_path = tmp_path / "meditation.md"
    script_path.write_text(
        "# Test\n\nWelcome to this moment.\n\n[pause: 57s]\n\nLet your shoulders soften.",
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
                "--delivery",
                "calm",
                "--intensity",
                "moderate",
                "--pace",
                "very_slow",
                "--requested-duration-seconds",
                "60",
                "--json",
            ]
        )
        == 0
    )

    completed = json.loads(capsys.readouterr().out)
    run_directory = runs_dir / completed["run_id"]
    assert completed["schema_version"] == 6
    assert completed["status"] == "completed"
    assert completed["source_kind"] == "script_file"
    assert (run_directory / "script.md").read_text(encoding="utf-8").startswith("# Test")
    assert (run_directory / "resolved-config.json").is_file()
    assert (run_directory / "model-metadata.json").is_file()
    assert (run_directory / "timeline.json").is_file()
    assert (run_directory / "narration.wav").is_file()
    assert (run_directory / "audio-manifest.json").is_file()
    resolved = json.loads((run_directory / "resolved-config.json").read_text(encoding="utf-8"))
    assert resolved["tts"]["delivery_override"] == "calm"
    assert resolved["tts"]["intensity_override"] == "moderate"
    assert resolved["tts"]["pace_override"] == "very_slow"
    assert resolved["requested_duration_seconds"] == 60
    timeline = json.loads((run_directory / "timeline.json").read_text(encoding="utf-8"))
    assert {
        segment["delivery"] for segment in timeline["segments"] if segment["type"] == "SPEECH"
    } == {"neutral"}
    assert {
        segment["intensity"] for segment in timeline["segments"] if segment["type"] == "SPEECH"
    } == {"subtle"}
    assert {segment["pace"] for segment in timeline["segments"] if segment["type"] == "SPEECH"} == {
        "natural"
    }
    quality = json.loads((run_directory / "quality.json").read_text(encoding="utf-8"))
    assert quality["passed"] is True
    requested_duration = next(
        check for check in quality["checks"] if check["name"] == "requested_duration"
    )
    assert requested_duration["passed"] is True
    assert len(synthesizers) == 2
    assert [synthesizer.close_calls for synthesizer in synthesizers] == [1, 1]


def test_global_pace_override_applies_to_every_speech_segment_only() -> None:
    timeline = Timeline(
        schema_version=3,
        run_id=RUN_ID,
        created_at=datetime.now(UTC),
        source="script_file",
        segments=[
            SpeechSegment(id="speech-1", text="Arrive.", pace="natural"),
            SpeechSegment(id="speech-2", text="Rest here.", pace="unhurried"),
        ],
    )
    settings = TTSRunSettings(
        voice_name="test",
        speaker_id=0,
        speed=0.6,
        num_threads=1,
        provider="cpu",
        language="en-us",
        pace_override="very_slow",
    )

    overridden = _apply_global_speech_overrides(timeline, settings)

    assert [
        segment.pace for segment in overridden.segments if isinstance(segment, SpeechSegment)
    ] == ["very_slow", "very_slow"]


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
                        "technique": (
                            "arrival"
                            if section_id == "arrive"
                            else "return"
                            if section_id == "return"
                            else "focused_attention"
                        ),
                        "weight": 1,
                        "pause_seconds": 6,
                    }
                    for section_id in ("arrive", "notice", "return")
                ],
            }
        else:
            section_id = request.prompt.split("Section ID: ", 1)[1].splitlines()[0]
            section_text = {
                "arrive": (
                    "Notice the chair supporting you now. "
                    "Choose a comfortable posture for this quiet moment. "
                    "Let the room's sounds mark your arrival."
                ),
                "notice": (
                    "Rest attention on the natural breath. "
                    "When the mind wanders, acknowledge it without judgment. "
                    "Return gently to the same physical anchor."
                ),
                "return": (
                    "Hear the sounds around you again. "
                    "Feel the contact beneath your feet. "
                    "Invite a small movement before continuing with your day."
                ),
            }
            value = {
                "section_id": section_id,
                "text": section_text[section_id],
            }
        return ScriptGenerationResult(
            text=json.dumps(value),
            metadata=self.metadata,
            elapsed_seconds=0.01,
        )


class _InterruptOnceDraft(_DraftFixture):
    def __init__(self) -> None:
        self.interruptions_remaining = 1

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        if self.interruptions_remaining:
            self.interruptions_remaining -= 1
            raise KeyboardInterrupt
        return super().generate(request)


@pytest.mark.parametrize("configuration", ["checkout", "installed", "explicit"])
def test_draft_command_persists_validated_local_generation(
    configuration: str,
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
    config_args: list[str] = []
    if configuration != "checkout":
        installed_prefix = tmp_path / "installed"
        installed_config = installed_prefix / "share" / "whoopy" / "config"
        shutil.copytree(Path("config"), installed_config)
        monkeypatch.setattr("sys.prefix", str(installed_prefix))
        if configuration == "explicit":
            selected_config = tmp_path / "selected-config"
            shutil.copytree(installed_config, selected_config)
            blueprint_path = selected_config / "meditation_blueprints.yaml"
            library = yaml.safe_load(blueprint_path.read_text())
            for blueprint in library["blueprints"]:
                blueprint["version"] = 9
            blueprint_path.write_text(yaml.safe_dump(library))
            config_args = ["--config-dir", str(selected_config)]
        monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "draft",
                "A gentle grounding pause.",
                *config_args,
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
    plan = json.loads((draft / "plan.json").read_text())
    assert plan["blueprint_id"] == f"grounding@{9 if configuration == 'explicit' else 1}"
    assert (draft / "request.json").is_file()
    assert (draft / "plan.json").is_file()
    assert (draft / "script.md").is_file()
    assert (draft / "timeline.json").is_file()
    assert len(list((draft / "sections").glob("*.json"))) == 3


@pytest.mark.parametrize(
    "backend",
    ["fish-1.4", "moss-local-v1.5", "moss-v1.5", *QWEN_BACKEND_VARIANTS, *MLX_QWEN_BACKENDS],
)
@pytest.mark.parametrize("mode", ["prompt", "script"])
def test_generate_rejects_unsupported_numeric_speed_before_backend_setup(
    backend: str,
    mode: str,
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    def unexpected_backend_setup(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Unsupported numeric speed reached model setup")

    monkeypatch.setattr("whoopy.cli._model_plan", unexpected_backend_setup)
    script = tmp_path / "script.md"
    script.write_text("Notice an ordinary sound.\n", encoding="utf-8")
    source = ["A gentle grounding pause."] if mode == "prompt" else ["--script-file", str(script)]
    runs_dir = tmp_path / "runs"

    with pytest.raises(SystemExit) as error:
        main(
            [
                "generate",
                *source,
                "--tts-model",
                backend,
                "--speed",
                "0.6",
                "--pacing-profile",
                "meditative-v2",
                "--runs-dir",
                str(runs_dir),
            ]
        )

    assert error.value.code == 2
    assert "does not support numeric --speed" in capsys.readouterr().err
    assert not runs_dir.exists()


@pytest.mark.parametrize(
    ("timing_args", "expected_speed", "expected_pacing"),
    [
        ([], 0.6, "meditative-v2"),
        (["--speed", "0.9"], 0.9, None),
        (["--speed", "0.9", "--pacing-profile", "meditative-v2"], 0.6, "meditative-v2"),
    ],
)
def test_generate_prompt_joins_validated_draft_to_real_audio_contract(
    timing_args: list[str],
    expected_speed: float,
    expected_pacing: str | None,
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
                *timing_args,
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
    resolved = json.loads((run / "resolved-config.json").read_text(encoding="utf-8"))
    assert resolved["tts"]["speed"] == expected_speed
    pacing = resolved["generation"]["pacing"]
    assert (pacing["profile_id"] if pacing is not None else None) == expected_pacing
    manifest = json.loads((run / "audio-manifest.json").read_text(encoding="utf-8"))
    assert manifest["duration_ms"] == pytest.approx(60_000, abs=1)
    assert timeline["preserve_timing"] is False
    assert completed["schema_version"] == 6
    assert completed["source_kind"] == "generated_prompt"
    assert completed["status"] == "completed"
    assert timeline["source"] == "generated_prompt"
    assert metadata["llm"]["adapter_id"] == "test.draft"
    assert (run / "plan.json").is_file()
    assert len(list((run / "draft-sections").glob("*.json"))) == 3
    # Whole-script review may request alternatives only for failed sections;
    # every original and repair attempt remains inspectable.
    assert len(list((run / "raw-model-output").glob("*.json"))) >= 4
    assert (run / "narration.wav").is_file()
    assert json.loads((run / "quality.json").read_text(encoding="utf-8"))["passed"] is True


def test_generate_adopts_an_explicit_preallocated_run_id(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    runs_dir = tmp_path / "runs"
    prompt = "A gentle grounding pause."
    RunStore(runs_dir).create(prompt, run_id=RUN_ID)
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: _standard_snapshot())
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

    assert (
        main(
            [
                "generate",
                prompt,
                "--run-id",
                str(RUN_ID),
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
    assert completed["run_id"] == str(RUN_ID)
    assert completed["status"] == "completed"
    store = RunStore(runs_dir)
    pending = store.load_generation_request(RUN_ID)
    assert isinstance(pending, PendingGenerationConfig)
    assert pending.writing_method == "plan_first"
    resolved_generation = store.load_resolved_config(RUN_ID).generation
    assert resolved_generation is not None
    assert resolved_generation.writing_method == "plan_first"


def test_generate_refuses_to_adopt_a_run_id_for_a_different_prompt(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runs_dir = tmp_path / "runs"
    store = RunStore(runs_dir)
    store.create("The original request.", run_id=RUN_ID)
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: _standard_snapshot())
    monkeypatch.setattr(
        SherpaOnnxKokoroAdapter,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: FixtureSpeechSynthesizer()),
    )

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "generate",
                "A different request.",
                "--run-id",
                str(RUN_ID),
                "--profile",
                "standard",
                "--models-dir",
                str(tmp_path / "models"),
                "--runs-dir",
                str(runs_dir),
            ]
        )

    assert store.load(RUN_ID).status is RunStatus.QUEUED
    assert not store.generation_request_path(RUN_ID).exists()


def test_generate_refuses_to_retag_a_preallocated_run_audience(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runs_dir = tmp_path / "runs"
    store = RunStore(runs_dir)
    store.create("A controlled comparison.", run_id=RUN_ID, audience="lab")
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: _standard_snapshot())
    monkeypatch.setattr(
        SherpaOnnxKokoroAdapter,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: FixtureSpeechSynthesizer()),
    )

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "generate",
                "A controlled comparison.",
                "--run-id",
                str(RUN_ID),
                "--profile",
                "standard",
                "--models-dir",
                str(tmp_path / "models"),
                "--runs-dir",
                str(runs_dir),
                "--audience",
                "system",
            ]
        )

    record = store.load(RUN_ID)
    assert record.status is RunStatus.QUEUED
    assert record.audience == "lab"
    assert not store.generation_request_path(RUN_ID).exists()


@pytest.mark.parametrize("explicit_speed", [None, 0.9])
def test_run_resume_reconciles_an_expired_planning_lease_and_reuses_the_request(
    explicit_speed: float | None,
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    runs_dir = tmp_path / "runs"
    draft = _InterruptOnceDraft()
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: _standard_snapshot())
    monkeypatch.setattr(
        LlamaCppScriptGenerator,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: draft),
    )
    monkeypatch.setattr(
        SherpaOnnxKokoroAdapter,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: FixtureSpeechSynthesizer()),
    )
    prompt = "A gentle grounding pause."
    generate_arguments = [
        "generate",
        prompt,
        "--run-id",
        str(RUN_ID),
        "--minutes",
        "1",
        "--profile",
        "standard",
        "--models-dir",
        str(tmp_path / "models"),
        "--runs-dir",
        str(runs_dir),
        "--audience",
        "lab",
        "--json",
    ]

    if explicit_speed is not None:
        generate_arguments.extend(["--speed", str(explicit_speed)])
    assert main(generate_arguments) == 130
    capsys.readouterr()
    store = RunStore(runs_dir)
    interrupted = store.load(RUN_ID)
    assert interrupted.status is RunStatus.INTERRUPTED
    store.restart_generation(
        RUN_ID,
        owner_id="expired-worker",
        pid=999_999,
        started_at=datetime(2020, 1, 1, tzinfo=UTC),
        lease_seconds=1,
    )

    assert (
        main(
            [
                "run",
                "resume",
                str(RUN_ID),
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
    assert completed["run_id"] == str(RUN_ID)
    assert completed["status"] == "completed"
    assert completed["recovery"]["resume_count"] == 2
    assert completed["audience"] == "lab"
    resolved = store.load_resolved_config(RUN_ID)
    assert resolved.tts.speed == (explicit_speed if explicit_speed is not None else 0.6)
    assert resolved.generation is not None
    assert (resolved.generation.pacing is None) is (explicit_speed is not None)


def test_run_reconcile_command_marks_an_expired_attempt_interrupted(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    store = RunStore(tmp_path)
    queued = store.create("A durable test.", run_id=RUN_ID)
    assert queued.recovery is not None
    started = datetime(2020, 1, 1, tzinfo=UTC)
    store.save(
        queued.transition(
            RunStatus.RUNNING,
            updated_at=started,
            recovery=queued.recovery.model_copy(update={"process_attempts": 1}),
            execution=RunExecution(
                stage=RunStage.SYNTHESIZING,
                attempt_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                owner_id="old-worker",
                pid=999_999,
                started_at=started,
                heartbeat_at=started,
                lease_expires_at=started + timedelta(seconds=15),
            ),
        )
    )

    assert (
        main(
            [
                "run",
                "reconcile",
                str(RUN_ID),
                "--runs-dir",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["runs"][0]["status"] == "interrupted"
    assert output["runs"][0]["execution"]["interruption_kind"] == "lease_expired"


def test_run_cancel_signals_only_a_locked_live_local_owner(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    monkeypatch: MonkeyPatch,
) -> None:
    store = RunStore(tmp_path)
    queued = store.create("A durable test.", run_id=RUN_ID)
    assert queued.recovery is not None
    now = datetime.now(UTC)
    target_pid = 999_999
    store.save(
        queued.transition(
            RunStatus.RUNNING,
            updated_at=now,
            recovery=queued.recovery.model_copy(update={"process_attempts": 1}),
            execution=RunExecution(
                stage=RunStage.SYNTHESIZING,
                attempt_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                owner_id=f"{socket.gethostname()}:{target_pid}",
                pid=target_pid,
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=15),
            ),
        )
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("whoopy.cli.os.kill", lambda pid, sig: signals.append((pid, sig)))

    with RunLock(store.run_directory(RUN_ID)):
        assert (
            main(
                [
                    "run",
                    "cancel",
                    str(RUN_ID),
                    "--runs-dir",
                    str(tmp_path),
                    "--json",
                ]
            )
            == 0
        )

    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == str(RUN_ID)
    assert signals == [(target_pid, 0), (target_pid, signal.SIGTERM)]


def test_run_cancel_refuses_to_signal_without_the_worker_lock(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = RunStore(tmp_path)
    queued = store.create("A durable test.", run_id=RUN_ID)
    assert queued.recovery is not None
    now = datetime.now(UTC)
    target_pid = 999_999
    store.save(
        queued.transition(
            RunStatus.RUNNING,
            updated_at=now,
            recovery=queued.recovery.model_copy(update={"process_attempts": 1}),
            execution=RunExecution(
                stage=RunStage.SYNTHESIZING,
                attempt_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
                owner_id=f"{socket.gethostname()}:{target_pid}",
                pid=target_pid,
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=15),
            ),
        )
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("whoopy.cli.os.kill", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "run",
                "cancel",
                str(RUN_ID),
                "--runs-dir",
                str(tmp_path),
                "--json",
            ]
        )

    assert signals == []
