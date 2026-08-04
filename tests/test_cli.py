from __future__ import annotations

import hashlib
import json
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
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.cli import main
from whoopy.hardware import HardwareSnapshot
from whoopy.pipeline.generation import PendingGenerationConfig
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.runs import RunExecution, RunStage, RunStatus, RunStore
from whoopy.ports import AdapterMetadata, ScriptGenerationRequest, ScriptGenerationResult

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
    assert completed["schema_version"] == 6
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
    assert len(synthesizers) == 2
    assert [synthesizer.close_calls for synthesizer in synthesizers] == [1, 1]


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


class _InterruptOnceDraft(_DraftFixture):
    def __init__(self) -> None:
        self.interruptions_remaining = 1

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        if self.interruptions_remaining:
            self.interruptions_remaining -= 1
            raise KeyboardInterrupt
        return super().generate(request)


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
    assert completed["schema_version"] == 6
    assert completed["source_kind"] == "generated_prompt"
    assert completed["status"] == "completed"
    assert timeline["source"] == "generated_prompt"
    assert metadata["llm"]["adapter_id"] == "test.draft"
    assert (run / "plan.json").is_file()
    assert len(list((run / "draft-sections").glob("*.json"))) == 3
    assert len(list((run / "raw-model-output").glob("*.json"))) == 4
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
    assert isinstance(RunStore(runs_dir).load_generation_request(RUN_ID), PendingGenerationConfig)


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


def test_run_resume_reconciles_an_expired_planning_lease_and_reuses_the_request(
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
        "--json",
    ]

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
