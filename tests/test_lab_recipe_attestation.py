from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from whoopy.evaluation import (
    EvaluationCondition,
    EvaluationLab,
    ExperimentCandidate,
    ExperimentDefinition,
    ProductionTrialRequest,
    VoiceConfiguration,
    load_evaluation_set,
)
from whoopy.ports import AdapterMetadata
from whoopy.webui.evaluation import LocalProductionExperimentExecutor


class CompletedGenerationApplication:
    def __init__(self, root: Path, *, tamper: str | None = None) -> None:
        self.runs_directory = root
        self.tamper = tamper
        self.tasks: dict[str, dict[str, Any]] = {}
        self.payloads: list[dict[str, Any]] = []

    def start_generation(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        run_id = uuid4()
        root = self.runs_directory / str(run_id)
        root.mkdir()
        recipe = payload["writing_method"] == "recipe_whole_session"
        plan = {
            "schema_version": 2,
            "blueprint_id": "daytime-steadying@1" if recipe else "grounding@1",
            "blueprint_version": 1,
        }
        if self.tamper == "recipe":
            plan["blueprint_id"] = "sleep-settling@1"
        if self.tamper == "version":
            plan["blueprint_version"] = 2
        moss = payload["tts_model"].startswith("moss-")
        tts = {
            "backend": payload["tts_model"],
            "seed": payload["seed"],
            "speed": 1.0 if moss else payload["speed"],
            "delivery_override": payload["delivery"],
            "intensity_override": payload["intensity"],
            "voice_mode": "designed" if moss else "preset",
            "voice_name": payload["voice"],
            "language": payload["moss_language"] if moss else "en-us",
            "reference_id": None,
            "use_reference": False,
            "instruction": payload.get("moss_instruction"),
        }
        if self.tamper == "instruction":
            tts["instruction"] = "An unrelated direction."
        metadata = AdapterMetadata(
            adapter_id="test.lab",
            versioned_model_id="qwen3-4b@fixture",
            runtime_id="python",
            runtime_version="test",
            license_id="CC0-1.0",
            device="fixture",
        ).model_dump(mode="json")
        artifacts = {
            "plan.json": plan,
            "run.json": {"prompt": payload["text"]},
            "timeline.json": {"segments": []},
            "audio-manifest.json": {"duration_ms": payload["minutes"] * 60_000},
            "quality.json": {"clipped_samples": 0},
            "model-metadata.json": {"llm": metadata, "tts": metadata},
            "resolved-config.json": {
                "mode": "generated_prompt",
                "profile": payload["profile"],
                "tts": tts,
                "generation": {
                    "seed": payload["seed"],
                    "duration_seconds": payload["minutes"] * 60,
                    "meditation_kind": payload["meditation_kind"],
                    "writing_method": payload["writing_method"],
                    "plan_prompt_id": "whoopy.plan",
                    "plan_prompt_version": 5,
                    "section_prompt_id": "whoopy.section",
                    "section_prompt_version": 3,
                },
            },
        }
        for filename, content in artifacts.items():
            (root / filename).write_text(json.dumps(content), encoding="utf-8")
        (root / "narration.wav").write_bytes(b"fixture-audio")
        task = {"task_id": str(run_id), "run_id": str(run_id), "status": "completed"}
        self.tasks[str(run_id)] = task
        return task

    def task(self, task_id: str) -> dict[str, Any]:
        return self.tasks[task_id]

    def environment_status(self) -> dict[str, str]:
        return {"system": "fixture"}


def _condition() -> EvaluationCondition:
    return EvaluationCondition(
        llm_model_id="qwen3-4b",
        writing_method="plan_first",
        voice=VoiceConfiguration(
            configuration_id="kokoro-calm",
            model_id="kokoro",
            voice_id="af_heart",
        ),
        pacing_profile_id="meditative-v2",
    )


def _request(condition: EvaluationCondition) -> ProductionTrialRequest:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    return ProductionTrialRequest(
        experiment_id="recipe-attestation",
        experiment_version=1,
        experiment_type="writing_method",
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="candidate",
        condition=condition,
        seed=42,
    )


def test_writing_comparison_keeps_request_fixed_and_preserves_recipe_provenance(
    tmp_path: Path,
) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    baseline = _condition()
    definition = ExperimentDefinition(
        experiment_id="recipe-comparison",
        version=1,
        experiment_type="writing_method",
        evaluation_id=evaluation.evaluation_id,
        evaluation_version=evaluation.version,
        seed=42,
        baseline_candidate_id="baseline",
        candidates=[
            ExperimentCandidate(candidate_id="baseline", label="Plan", condition=baseline),
            ExperimentCandidate(
                candidate_id="recipe",
                label="Recipe",
                condition=baseline.model_copy(update={"writing_method": "recipe_whole_session"}),
            ),
        ],
    )
    application = CompletedGenerationApplication(tmp_path)
    executor = LocalProductionExperimentExecutor(cast(Any, application))

    report = EvaluationLab(evaluation_set=evaluation, platform="fixture").run(
        definition, executor, case_ids=[evaluation.cases[0].id]
    )

    assert report.comparison_status == "complete"
    assert all(trial.success for trial in report.trials)
    fingerprints = [trial.controlled_inputs for trial in report.trials]
    assert fingerprints[0] is not None and fingerprints[1] is not None
    assert fingerprints[0].blueprint_sha256 == fingerprints[1].blueprint_sha256
    plans = [
        json.loads(Path(next(item.path for item in trial.artifacts if item.role == "plan")).read_text())
        for trial in report.trials
    ]
    assert [plan["blueprint_id"] for plan in plans] == ["grounding@1", "daytime-steadying@1"]


@pytest.mark.parametrize("tamper", ["recipe", "version"])
def test_recipe_attestation_rejects_wrong_output_provenance(tmp_path: Path, tamper: str) -> None:
    request = _request(_condition().model_copy(update={"writing_method": "recipe_whole_session"}))
    executor = LocalProductionExperimentExecutor(
        cast(Any, CompletedGenerationApplication(tmp_path, tamper=tamper))
    )

    result = executor.execute(request)

    assert not result.success
    assert result.failure is not None and "source.plan.blueprint" in result.failure


@pytest.mark.parametrize("instruction", [None, "Speak with a soft, steady low voice."])
@pytest.mark.parametrize("tamper", [None, "instruction"])
def test_moss_instruction_attests_the_submitted_control(
    tmp_path: Path, instruction: str | None, tamper: str | None
) -> None:
    voice = VoiceConfiguration(
        configuration_id="moss-designed",
        model_id="moss-5b",
        voice_id="direct",
        voice_mode="designed",
        instruction=instruction,
    )
    request = _request(_condition().model_copy(update={"voice": voice}))
    application = CompletedGenerationApplication(tmp_path, tamper=tamper)
    executor = LocalProductionExperimentExecutor(cast(Any, application))

    result = executor.execute(request)

    assert result.success == (tamper is None)
    if instruction is not None:
        assert application.payloads[0]["moss_instruction"] == instruction
    if tamper is not None:
        assert result.failure is not None and "tts.instruction" in result.failure
