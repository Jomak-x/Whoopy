from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from whoopy.evaluation import (
    EvaluationCondition,
    EvaluationExecutionError,
    ExperimentCandidate,
    ExperimentDefinition,
    ProductionTrialRequest,
    VoiceConfiguration,
    load_evaluation_set,
)
from whoopy.pipeline import RunStore
from whoopy.webui.api import _LabApiService
from whoopy.webui.evaluation import LocalProductionExperimentExecutor
from whoopy.webui.server import GenerationTask, LocalWebApplication


def _definition(name: str = "boundary") -> ExperimentDefinition:
    condition = EvaluationCondition(
        llm_model_id="qwen3-4b",
        writing_method="plan_first",
        voice=VoiceConfiguration(
            configuration_id="qwen", model_id="qwen3-custom-voice-0.6b", voice_id="Ryan"
        ),
        pacing_profile_id="meditative-v2",
    )
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    return ExperimentDefinition(
        experiment_id=name,
        version=1,
        experiment_type="llm",
        evaluation_id=evaluation.evaluation_id,
        evaluation_version=evaluation.version,
        seed=42,
        baseline_candidate_id="baseline",
        candidates=[
            ExperimentCandidate(candidate_id="baseline", label="Baseline", condition=condition),
            ExperimentCandidate(
                candidate_id="candidate",
                label="Candidate",
                condition=condition.model_copy(update={"llm_model_id": "qwen3-1.7b"}),
            ),
        ],
    )


class RefusingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: ProductionTrialRequest) -> Any:
        self.calls += 1
        raise RuntimeError("fixture refuses inference")


def _service(tmp_path: Path, executor: RefusingExecutor) -> _LabApiService:
    application = SimpleNamespace(
        runs_directory=tmp_path,
        config_directory=Path("config"),
        environment_status=lambda: {"system": "fixture"},
    )
    return _LabApiService(application=cast(Any, application), executor=executor)


def test_windows_control_persistence_allows_lab_worker_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whoopy.evaluation import storage
    from whoopy.pipeline import runs

    file_flushes: list[int] = []
    directory_opens: list[Path] = []

    def flush(descriptor: int) -> None:
        file_flushes.append(descriptor)
        os.fsync(descriptor)

    def open_directory(path: Path, flags: int) -> int:
        directory_opens.append(path)
        raise PermissionError("Windows cannot open a directory with os.open")

    windows = SimpleNamespace(**(vars(os) | {"name": "nt", "open": open_directory, "fsync": flush}))
    monkeypatch.setattr(storage, "os", windows)
    monkeypatch.setattr(runs, "os", windows)
    executor = RefusingExecutor()
    service = _service(tmp_path, executor)
    definition = _definition()
    service.add_definition(definition)

    accepted = service.execute(experiment_id=definition.experiment_id, version=1, case_ids=None)
    worker = service._worker
    if worker is not None:
        worker.join(5)
        assert not worker.is_alive()

    assert accepted["accepted"] is True
    assert executor.calls == 1
    assert directory_opens == []
    assert len(file_flushes) >= 3
    persisted = service.repository.load_execution_control(
        experiment_id=definition.experiment_id, version=1
    )
    assert persisted is not None
    assert persisted["status"] == "incomplete"
    assert persisted["failure"] == "RuntimeError: fixture refuses inference"
    assert not list(service.repository.root.rglob("*.tmp"))


def test_new_execution_starts_while_idle_worker_thread_is_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = RefusingExecutor()
    service = _service(tmp_path, executor)
    release = threading.Event()
    returned = threading.Event()
    threads: list[threading.Thread] = []
    original_thread = threading.Thread

    class ExitingThread(original_thread):
        def run(self) -> None:
            try:
                super().run()
            finally:
                returned.set()
                release.wait(5)

    def start_worker(*args: Any, **kwargs: Any) -> threading.Thread:
        thread = ExitingThread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr("whoopy.webui.api.threading.Thread", start_worker)
    first = _definition("first")
    second = _definition("second")
    service.add_definition(first)
    service.add_definition(second)
    try:
        service.execute(experiment_id="first", version=1, case_ids=None)
        assert returned.wait(5)
        assert threads[0].is_alive()
        result = service.execute(experiment_id="second", version=1, case_ids=None)
        assert result["status"] == "running"
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert executor.calls == 2
    assert [entry.status for entry in service.queue.entries()] == ["failed", "failed"]


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_halt_survives_queue_start_before_control_transition(
    tmp_path: Path, action: Literal["pause", "cancel"]
) -> None:
    executor = RefusingExecutor()
    service = _service(tmp_path, executor)
    definition = _definition()
    service.add_definition(definition)
    key = (definition.experiment_id, definition.version)
    service._save_control(key, service._new_control(definition, None))
    service.queue.enqueue(definition)
    assert service.queue.start_next() is not None

    service.halt(experiment_id=key[0], version=key[1], action=action)
    service._drain_queue(definition)

    target = "paused" if action == "pause" else "cancelled"
    assert executor.calls == 0
    assert service._controls[key]["status"] == target
    persisted = service.repository.load_execution_control(experiment_id=key[0], version=key[1])
    assert persisted is not None
    assert persisted["status"] == target
    assert service.queue.entries()[0].failure == f"batch {target}"


@pytest.mark.parametrize("action", ["pause", "cancel"])
@pytest.mark.parametrize("halt_before_publication", [True, False])
def test_active_run_publication_honors_pending_halt(
    tmp_path: Path, action: Literal["pause", "cancel"], halt_before_publication: bool
) -> None:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    executor = LocalProductionExperimentExecutor(application)
    service = _LabApiService(application=application, executor=executor)
    definition = _definition()
    service.add_definition(definition)
    key = (definition.experiment_id, definition.version)
    control = service._new_control(definition, None)
    control["status"] = "running"
    service._save_control(key, control)
    record = RunStore(application.runs_directory).create("A newly launching trial")
    run_id = str(record.run_id)
    task = GenerationTask(task_id=run_id, run_id=run_id)
    application._tasks[run_id] = task
    assert executor.active_run_changed is not None

    if halt_before_publication:
        service.halt(experiment_id=key[0], version=key[1], action=action)
        assert task.cancel_requested is False
    executor.active_run_changed(record.run_id)
    if not halt_before_publication:
        service.halt(experiment_id=key[0], version=key[1], action=action)

    assert task.cancel_requested is True
    persisted = service.repository.load_execution_control(experiment_id=key[0], version=key[1])
    assert persisted is not None
    assert persisted["active_run_id"] == run_id
    assert persisted["halt_action"] == action
    executor.active_run_changed(None)
    assert service._controls[key]["active_run_id"] == run_id


def test_delivery_comparison_rejects_qwen_without_expressive_controls() -> None:
    original = _definition()
    first = original.candidates[0]
    second = first.model_copy(
        update={
            "candidate_id": "candidate",
            "condition": first.condition.model_copy(
                update={"voice": first.condition.voice.model_copy(update={"delivery": "sleepy"})}
            ),
        }
    )
    definition = ExperimentDefinition.model_validate(
        original.model_copy(
            update={"experiment_type": "delivery", "candidates": [first, second]}
        ).model_dump()
    )
    with pytest.raises(EvaluationExecutionError, match="expressive"):
        LocalProductionExperimentExecutor.validate_definition(definition)


@pytest.mark.parametrize(
    "backend", ["qwen3-custom-voice-0.6b", "qwen3-base-clone-0.6b", "qwen3-base-clone-1.7b"]
)
def test_qwen_fallback_controls_are_attested_as_rendered(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    condition = _definition().candidates[0].condition
    mode: Literal["preset", "reference"] = "reference" if "clone" in backend else "preset"
    condition = condition.model_copy(
        update={
            "voice": condition.voice.model_copy(update={"model_id": backend, "voice_mode": mode})
        }
    )
    case = load_evaluation_set(Path("config/evaluation/local-v1.yaml")).cases[0]
    request = ProductionTrialRequest(
        experiment_id="fallback",
        experiment_version=1,
        experiment_type="llm",
        trial_id=uuid4(),
        case=case,
        candidate_id="baseline",
        condition=condition,
        seed=42,
    )
    executor = LocalProductionExperimentExecutor(cast(Any, object()))
    monkeypatch.setattr(executor, "_validate_managed_reference", lambda reference: None)
    payload = executor._generation_payload(request)
    assert (payload["delivery"], payload["intensity"], payload["pace"]) == (
        "neutral",
        "subtle",
        "natural",
    )
    resolved: dict[str, Any] = {
        "mode": "generated_prompt",
        "profile": "standard",
        "tts": {
            "backend": backend,
            "seed": 42,
            "speed": 1.0,
            "delivery_override": payload["delivery"],
            "intensity_override": payload["intensity"],
            "voice_mode": mode,
            "voice_name": "Ryan",
            "reference_id": "Ryan",
            "language": "English",
        },
    }
    kwargs: dict[str, Any] = {
        "resolved": resolved,
        "model_metadata": {"llm": {"versioned_model_id": "qwen3-4b@fixture"}},
        "source_resolved": {
            "generation": {
                "seed": 42,
                "writing_method": "plan_first",
                "duration_seconds": case.duration_seconds,
                "meditation_kind": case.category,
            }
        },
        "source_prompt": case.prompt,
        "source_plan": {"blueprint_id": case.blueprint_id},
    }
    executor._attest_candidate_controls(request, **kwargs)
    resolved["tts"]["delivery_override"] = "sleepy"
    with pytest.raises(ValueError, match="delivery_override"):
        executor._attest_candidate_controls(request, **kwargs)


@pytest.mark.parametrize("refuse_ownership", [False, True])
def test_generation_persists_lab_ownership_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refuse_ownership: bool
) -> None:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    monkeypatch.setattr(application, "_require_speech_model_ready", lambda *args, **kwargs: None)
    owned: list[str] = []
    launched = threading.Event()

    def own(run_id: Any) -> None:
        assert application._load_web_launch_request(str(run_id))["text"] == "Rest here."
        owned.append(str(run_id))
        if refuse_ownership:
            raise OSError("fixture cannot persist ownership")

    def launch(task: GenerationTask, *args: Any) -> None:
        assert owned == [task.run_id]
        launched.set()

    monkeypatch.setattr(application, "_run_generation", launch)
    payload = {"mode": "script", "text": "Rest here.", "tts_model": "kokoro"}
    if refuse_ownership:
        with pytest.raises(OSError, match="persist ownership"):
            application.start_generation(payload, _run_created=own)
        assert not launched.is_set()
        assert application._tasks == {}
        failed_run = application.run(owned[0])
        assert failed_run is not None
        assert failed_run["status"] == "failed"
    else:
        task = application.start_generation(payload, _run_created=own)
        assert launched.wait(5)
        assert task["run_id"] == owned[0]
