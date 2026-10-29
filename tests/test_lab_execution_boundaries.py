from __future__ import annotations

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
from whoopy.webui.api import _LabApiService
from whoopy.webui.evaluation import LocalProductionExperimentExecutor


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
