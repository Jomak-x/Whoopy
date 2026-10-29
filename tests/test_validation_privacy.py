from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import pytest

from tests.test_recipe_whole_session import RUN_ID, WholeSessionAdapter, run
from whoopy.adapters.llm import LlamaCppScriptGenerator
from whoopy.adapters.tts import SherpaOnnxKokoroAdapter
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.cli import main
from whoopy.hardware import HardwareSnapshot
from whoopy.meditation import GenerationError, GenerationWorkspace
from whoopy.meditation.models import RawGenerationAttempt
from whoopy.meditation.whole_session import WholeSessionDraft
from whoopy.pipeline import RunStatus, RunStore
from whoopy.webui.server import LocalWebApplication

PRIVATE_OUTPUT = "PRIVATE-MODEL-OUTPUT-SENTINEL"


@pytest.mark.parametrize(
    ("payload", "public_detail", "private_detail"),
    [
        (json.dumps({"sections": PRIVATE_OUTPUT}), "sections: list_type", "input_value"),
        (json.dumps({PRIVATE_OUTPUT: "value"}), "<field>: extra_forbidden", PRIVATE_OUTPUT),
        (
            '{"' + PRIVATE_OUTPUT + '": 1, "' + PRIVATE_OUTPUT + '": 2}',
            "ValueError",
            "duplicate JSON field",
        ),
    ],
)
def test_rejected_output_stays_private_in_cli_record_and_run_summary(
    payload: str,
    public_detail: str,
    private_detail: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = WholeSessionAdapter(envelope=lambda _text: payload)
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
        classmethod(lambda _cls, **_kwargs: adapter),
    )
    monkeypatch.setattr(
        SherpaOnnxKokoroAdapter,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: FixtureSpeechSynthesizer()),
    )
    runs_directory = tmp_path / "runs"
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "generate",
                "A gentle grounding pause.",
                "--minutes",
                "1",
                "--profile",
                "standard",
                "--writing-method",
                "recipe_whole_session",
                "--run-id",
                str(RUN_ID),
                "--models-dir",
                str(tmp_path / "models"),
                "--runs-dir",
                str(runs_directory),
            ]
        )
    assert exited.value.code == 2
    assert len(adapter.requests) == 2
    cli_error = capsys.readouterr().err
    assert public_detail in cli_error
    assert PRIVATE_OUTPUT not in cli_error
    assert "input_value" not in cli_error

    store = RunStore(runs_directory)
    record = store.load(RUN_ID)
    assert record.status is RunStatus.FAILED
    assert record.error is not None and public_detail in record.error
    assert PRIVATE_OUTPUT not in record.model_dump_json()
    assert "input_value" not in record.model_dump_json()
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=tmp_path / "models",
        runs_directory=runs_directory,
    )
    summary = application.run(str(RUN_ID))
    assert summary is not None and summary["error"] == record.error
    assert PRIVATE_OUTPUT not in json.dumps(summary)
    assert "input_value" not in json.dumps(summary)
    assert main(["run", "show", str(RUN_ID), "--runs-dir", str(runs_directory)]) == 0
    displayed_run = capsys.readouterr().out
    assert public_detail in displayed_run
    assert PRIVATE_OUTPUT not in displayed_run
    assert "input_value" not in displayed_run

    workspace = runs_directory / ".generation-workspaces" / str(RUN_ID)
    attempts = [
        RawGenerationAttempt.model_validate_json(path.read_text())
        for path in sorted((workspace / "raw-model-output").glob("*.json"))
    ]
    assert len(attempts) == 2
    assert [attempt.text for attempt in attempts] == [payload, payload]
    assert all(private_detail in (attempt.validation_error or "") for attempt in attempts)
    assert all(attempt.request_seed in (42, 43) for attempt in attempts)
    assert not (workspace / "whole-session.json").exists()


def test_invalid_checkpoint_retains_private_validation_evidence(tmp_path: Path) -> None:
    workspace = GenerationWorkspace(tmp_path / "draft")
    saved = {"sections": PRIVATE_OUTPUT}
    workspace.save_whole_session(saved)
    adapter = WholeSessionAdapter()

    with pytest.raises(GenerationError) as rejected:
        run(adapter, workspace=workspace)

    assert "sections: list_type" in str(rejected.value)
    assert PRIVATE_OUTPUT not in str(rejected.value)
    assert "input_value" not in str(rejected.value)
    assert rejected.value.__suppress_context__
    assert not adapter.requests
    assert workspace.load_whole_session() == saved
    evidence = list((workspace.root / "raw-model-output").glob("*.json"))
    assert len(evidence) == 1
    attempt = RawGenerationAttempt.model_validate_json(evidence[0].read_text())
    assert json.loads(attempt.text) == saved
    assert attempt.validation_error is not None
    assert PRIVATE_OUTPUT in attempt.validation_error
    assert "input_value" in attempt.validation_error


def test_custom_validator_exception_is_local_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(*_args: object, **_kwargs: object) -> NoReturn:
        raise ValueError(f"Rejected text: {PRIVATE_OUTPUT}")

    monkeypatch.setattr(WholeSessionDraft, "model_validate", reject)
    workspace = GenerationWorkspace(tmp_path / "draft")
    adapter = WholeSessionAdapter()
    with pytest.raises(GenerationError) as rejected:
        run(adapter, workspace=workspace)

    assert PRIVATE_OUTPUT not in str(rejected.value)
    assert "ValueError" in str(rejected.value)
    assert len(adapter.requests) == 2
    attempts = [
        RawGenerationAttempt.model_validate_json(path.read_text())
        for path in sorted((workspace.root / "raw-model-output").glob("*.json"))
    ]
    assert len(attempts) == 2
    assert all(PRIVATE_OUTPUT in (attempt.validation_error or "") for attempt in attempts)
    assert [json.loads(attempt.text) for attempt in attempts] == adapter.outputs
