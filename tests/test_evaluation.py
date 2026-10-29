from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from scripts.meditation_corpus_report import inspect_case
from whoopy.evaluation import (
    ArtifactReference,
    BakeoffRunner,
    CanonicalSourceProvenance,
    ControlledInputFingerprints,
    EvaluationCondition,
    EvaluationExecutionError,
    EvaluationExecutionHalted,
    EvaluationLab,
    EvaluationRepository,
    EvaluationSetError,
    ExperimentCandidate,
    ExperimentDefinition,
    HumanRating,
    MetricValue,
    ProductionTrialRequest,
    ProductionTrialResult,
    PromotionDecision,
    SequentialExperimentQueue,
    VoiceConfiguration,
    add_rating,
    blind_presentations,
    blind_review_status,
    compare_against_baseline,
    create_promotion_decision,
    load_evaluation_set,
    resolve_blind_audio,
)
from whoopy.evaluation.runner import _PeakProcessTree
from whoopy.meditation import load_prompt_bundle
from whoopy.pipeline.locks import RunLock
from whoopy.ports import (
    AdapterMetadata,
    ScriptGenerationRequest,
    ScriptGenerationResult,
)
from whoopy.timeline import build_script_timeline
from whoopy.webui.api import GenerationRequest
from whoopy.webui.evaluation import LocalProductionExperimentExecutor
from whoopy.webui.server import LocalWebApplication


class EvaluationFixtureGenerator:
    metadata = AdapterMetadata(
        adapter_id="test.evaluation",
        versioned_model_id="fixture@1",
        runtime_id="python",
        runtime_version="test",
        license_id="CC0-1.0",
        device="fixture",
    )

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        if "Create the structural JSON plan" in request.prompt:
            value = {
                "title": "A Measured Pause",
                "intention": "Offer an invitational practice.",
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
            match = re.search(r"Word range: (\d+)-(\d+)", request.prompt)
            assert match is not None
            _minimum_words, maximum_words = (int(value) for value in match.groups())
            word_count = maximum_words - 5
            opening = (
                ["You", "might", "notice", "the", "support"]
                if section_id == "return"
                else ["You", "might"]
            )
            words = [*opening, *([section_id] * (word_count - len(opening)))]
            sentences = [
                " ".join(words[index : index + 10]) + "." for index in range(0, len(words), 10)
            ]
            value = {"section_id": section_id, "text": " ".join(sentences)}
        return ScriptGenerationResult(
            text=json.dumps(value),
            metadata=self.metadata,
            elapsed_seconds=0.01,
        )


def test_versioned_evaluation_set_has_required_categories() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/phase-3-5.yaml"))

    assert evaluation.version == 1
    assert {case.category for case in evaluation.cases} == {
        "sleep",
        "grounding",
        "anxiety",
        "body_scan",
        "breath_awareness",
        "focus",
    }


def test_local_v1_evaluation_set_is_the_reviewed_ten_case_corpus() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))

    assert evaluation.schema_version == 2
    assert len(evaluation.cases) == 10
    assert all(case.blueprint_id is not None for case in evaluation.cases)
    assert {case.category for case in evaluation.cases} == {
        "grounding",
        "focused_attention",
        "breath_awareness",
        "body_scan",
        "noting",
        "reflection",
        "loving_kindness",
        "self_compassion",
        "open_awareness",
        "sleep",
    }


def test_invalid_evaluation_set_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: 1\ncases: []\n", encoding="utf-8")

    with pytest.raises(EvaluationSetError):
        load_evaluation_set(path)


def test_bakeoff_keeps_individual_metrics_without_total_score(tmp_path: Path) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/phase-3-5.yaml"))
    evaluation = evaluation.model_copy(update={"cases": evaluation.cases[:1]})
    adapter = EvaluationFixtureGenerator()
    runner = BakeoffRunner(
        evaluation_set=evaluation,
        prompts=load_prompt_bundle(Path("config/prompts")),
        output_directory=tmp_path,
    )

    results = runner.run_candidate(
        profile="lite",
        adapter=adapter,
        model_artifact_bytes=1_000,
    )
    report = runner.report(
        platform="test-x86_64",
        candidates={"lite": adapter},
        results=results,
    )

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].writing_method == "plan_first"
    assert results[0].safety_validation_passed is True
    assert results[0].timing_error_percent is not None
    assert results[0].invitational_phrase_count == 3
    assert report.human_review_status == "pending"
    assert report.writing_method == "plan_first"
    assert "score" not in report.model_dump()


def test_peak_memory_sampling_falls_back_to_current_process_when_children_are_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RestrictedProcess:
        def children(self, *, recursive: bool) -> list[object]:
            assert recursive is True
            raise PermissionError("sandbox denied process enumeration")

        def memory_info(self) -> SimpleNamespace:
            return SimpleNamespace(rss=12_345)

    monkeypatch.setattr("whoopy.evaluation.runner.psutil.Process", RestrictedProcess)

    with _PeakProcessTree() as memory:
        time.sleep(0.05)

    assert memory.peak_bytes == 12_345


def test_meditation_corpus_receipt_keeps_exact_production_failure(tmp_path: Path) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    result = SimpleNamespace(
        profile="lite",
        success=False,
        failure="GenerationError: unresolved repeated idea in ending",
    )

    receipt = inspect_case(
        case=evaluation.cases[0],
        result=result,
        artifact_root=tmp_path,
    )

    assert receipt["automatic_passed"] is False
    assert receipt["issues"] == [
        {
            "code": "production_failure",
            "message": "GenerationError: unresolved repeated idea in ending",
        },
        {
            "code": "missing_artifact",
            "message": "Missing artifacts: plan, script, timeline",
        },
    ]


def _condition(
    *,
    llm: str = "qwen3-4b",
    method: Literal[
        "plan_first",
        "blueprint_first_sparse",
        "three_candidate_structural_selection",
    ] = "blueprint_first_sparse",
    voice: str = "kokoro-heart-calm",
    pacing: str = "meditative-v2",
) -> EvaluationCondition:
    return EvaluationCondition(
        llm_model_id=llm,
        writing_method=method,
        voice=VoiceConfiguration(
            configuration_id=voice,
            model_id="kokoro",
            voice_id="af_heart",
            delivery="calm",
        ),
        pacing_profile_id=pacing,
    )


def test_kokoro_lab_payload_excludes_other_adapter_controls() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    request = ProductionTrialRequest(
        experiment_id="kokoro-routing",
        experiment_version=1,
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="kokoro",
        condition=_condition(method="plan_first"),
        seed=42,
    )
    executor = LocalProductionExperimentExecutor(cast(Any, object()))

    payload = executor._generation_payload(request)
    validated = GenerationRequest.model_validate(payload)

    assert validated.tts_model == "kokoro"
    assert validated.qwen_speaker is None
    assert validated.qwen_language is None
    assert validated.moss_language is None
    assert validated.voice_reference is None


@pytest.mark.parametrize(
    ("voice", "expected_backend", "expected_controls"),
    [
        (
            VoiceConfiguration(
                configuration_id="fish-reference",
                model_id="fish-1.4",
                voice_id="reference-1",
                voice_mode="reference",
                delivery="warm",
            ),
            "fish-1.4",
            {"voice_reference": "reference-1"},
        ),
        (
            VoiceConfiguration(
                configuration_id="moss-5b-reference",
                model_id="moss-5b",
                voice_id="reference-1",
                voice_mode="reference",
                delivery="calm",
                intensity="moderate",
                language="German",
                instruction="Speak with restrained warmth and crisp consonants.",
            ),
            "moss-local-v1.5",
            {
                "voice_reference": "reference-1",
                "moss_language": "German",
                "moss_instruction": "Speak with restrained warmth and crisp consonants.",
                "moss_use_reference": True,
            },
        ),
        (
            VoiceConfiguration(
                configuration_id="moss-8b-designed",
                model_id="moss-8b",
                voice_id="direct-voice",
                voice_mode="designed",
                delivery="sleepy",
                intensity="moderate",
                language="English",
            ),
            "moss-v1.5",
            {"voice_reference": None, "moss_use_reference": False},
        ),
        (
            VoiceConfiguration(
                configuration_id="qwen-custom-small",
                model_id="qwen3-custom-voice-0.6b",
                voice_id="Serena",
                voice_mode="preset",
                delivery="neutral",
                language="English",
            ),
            "qwen3-custom-voice-0.6b",
            {"qwen_language": "English", "qwen_speaker": "Serena"},
        ),
        (
            VoiceConfiguration(
                configuration_id="qwen-custom-large",
                model_id="qwen3-custom-voice-1.7b",
                voice_id="Serena",
                voice_mode="preset",
                language="Chinese",
            ),
            "qwen3-custom-voice-1.7b",
            {"qwen_language": "Chinese", "qwen_speaker": "Serena"},
        ),
        (
            VoiceConfiguration(
                configuration_id="qwen-design",
                model_id="qwen3-voice-design-1.7b",
                voice_id="reviewed-design",
                voice_mode="designed",
                delivery="sleepy",
                language="English",
            ),
            "qwen3-voice-design-1.7b",
            {
                "qwen_language": "English",
                "qwen_voice_design_preset": "sleep_meditation",
            },
        ),
        (
            VoiceConfiguration(
                configuration_id="qwen-clone-small",
                model_id="qwen3-base-clone-0.6b",
                voice_id="reference-1",
                voice_mode="reference",
                delivery="neutral",
                language="English",
            ),
            "qwen3-base-clone-0.6b",
            {"qwen_language": "English", "qwen_reference": "reference-1"},
        ),
        (
            VoiceConfiguration(
                configuration_id="qwen-clone-large",
                model_id="qwen3-base-clone-1.7b",
                voice_id="reference-1",
                voice_mode="reference",
                delivery="neutral",
                language="English",
            ),
            "qwen3-base-clone-1.7b",
            {"qwen_language": "English", "qwen_reference": "reference-1"},
        ),
    ],
)
def test_lab_routes_each_production_voice_contract_without_foreign_controls(
    monkeypatch: pytest.MonkeyPatch,
    voice: VoiceConfiguration,
    expected_backend: str,
    expected_controls: dict[str, object],
) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    request = ProductionTrialRequest(
        experiment_id="voice-routing",
        experiment_version=1,
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="voice",
        condition=_condition(method="plan_first").model_copy(update={"voice": voice}),
        seed=42,
    )
    executor = LocalProductionExperimentExecutor(cast(Any, object()))
    checked_references: list[str] = []
    monkeypatch.setattr(
        executor,
        "_validate_managed_reference",
        checked_references.append,
    )

    payload = executor._generation_payload(request)
    validated = GenerationRequest.model_validate(payload)

    assert validated.tts_model == expected_backend
    for field, value in expected_controls.items():
        assert getattr(validated, field) == value
    if not expected_backend.startswith("moss-"):
        assert validated.moss_language is None
        assert validated.moss_instruction is None
        assert validated.moss_use_reference is None
    if not expected_backend.startswith("qwen3-"):
        assert validated.qwen_language is None
        assert validated.qwen_speaker is None
        assert validated.qwen_voice_design_preset is None
        assert validated.qwen_reference is None
    expects_reference_check = expected_backend in {
        "fish-1.4",
        "moss-local-v1.5",
        "qwen3-base-clone-0.6b",
        "qwen3-base-clone-1.7b",
    }
    assert checked_references == (["reference-1"] if expects_reference_check else [])


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        (_condition(llm="prefix-qwen3-4b-unreviewed"), "LLM"),
        (
            _condition().model_copy(
                update={
                    "voice": VoiceConfiguration(
                        configuration_id="lookalike",
                        model_id="not-kokoro-compatible",
                        voice_id="af_heart",
                    )
                }
            ),
            "Voice model",
        ),
        (
            _condition().model_copy(
                update={
                    "voice": VoiceConfiguration(
                        configuration_id="kokoro-moderate",
                        model_id="kokoro",
                        voice_id="af_heart",
                        intensity="moderate",
                    )
                }
            ),
            "does not expose intensity",
        ),
        (
            _condition().model_copy(
                update={
                    "voice": VoiceConfiguration(
                        configuration_id="kokoro-german",
                        model_id="kokoro",
                        voice_id="af_heart",
                        language="German",
                    )
                }
            ),
            "require English",
        ),
    ],
)
def test_lab_rejects_unrouted_or_silently_ignored_controls(
    condition: EvaluationCondition,
    message: str,
) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    request = ProductionTrialRequest(
        experiment_id="invalid-routing",
        experiment_version=1,
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="candidate",
        condition=condition,
        seed=42,
    )

    with pytest.raises(ValueError, match=message):
        LocalProductionExperimentExecutor(cast(Any, object()))._generation_payload(request)


@pytest.mark.parametrize(
    ("llm", "profile"),
    [
        ("qwen3-4b", "standard"),
        ("qwen3-4b-standard", "standard"),
        ("qwen3-1.7b", "lite"),
        ("qwen3-1.7b-lite", "lite"),
    ],
)
def test_lab_llm_aliases_and_pacing_profiles_reach_production_controls(
    llm: str,
    profile: str,
) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    request = ProductionTrialRequest(
        experiment_id="llm-pacing-routing",
        experiment_version=1,
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="candidate",
        condition=_condition(llm=llm, pacing="progressive-quiet-v2"),
        seed=93,
    )

    payload = LocalProductionExperimentExecutor(cast(Any, object()))._generation_payload(request)

    assert payload["profile"] == profile
    assert payload["speed"] == 0.55
    assert payload["seed"] == 93
    assert payload["meditation_kind"] == request.case.category


def test_voice_challenger_freezes_accepted_script_controls_and_silence(
    tmp_path: Path,
) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    source_run_id = uuid4()
    root = tmp_path / str(source_run_id)
    root.mkdir()
    (root / "script.md").write_text(
        "[delivery: calm]\n\nFirst sentence.\n\n[pause: 2s]\n\nSecond sentence.\n",
        encoding="utf-8",
    )
    (root / "timeline.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "type": "SPEECH",
                        "text": "First sentence.",
                        "delivery": "calm",
                        "intensity": "subtle",
                        "pace": "slow",
                        "voice_mode": "preset",
                        "pronunciation_overrides": [],
                    },
                    {"type": "SILENCE", "duration_ms": 4321},
                    {
                        "type": "SPEECH",
                        "text": "Second sentence.",
                        "delivery": "grounded",
                        "intensity": "subtle",
                        "pace": "unhurried",
                        "voice_mode": "preset",
                        "pronunciation_overrides": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    application = SimpleNamespace(runs_directory=tmp_path)
    request = ProductionTrialRequest(
        experiment_id="frozen-voice",
        experiment_version=2,
        experiment_type="voice",
        controlled_source_run_id=source_run_id,
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="challenger",
        condition=_condition(),
        seed=42,
    )

    payload = LocalProductionExperimentExecutor(cast(Any, application))._generation_payload(request)

    assert payload["mode"] == "script"
    assert payload["profile"] == "basic"
    assert payload["speed"] == 0.6
    assert payload["requested_duration_seconds"] == request.case.duration_seconds
    assert "[pause: 4321ms]" in payload["text"]
    assert "First sentence." in payload["text"]
    assert "Second sentence." in payload["text"]
    compiled = build_script_timeline(
        run_id=uuid4(),
        script=cast(str, payload["text"]),
        created_at=datetime.now(UTC),
    )
    speech = [segment for segment in compiled.segments if segment.type == "SPEECH"]
    assert [segment.text for segment in speech] == ["First sentence.", "Second sentence."]
    assert [(segment.delivery, segment.pace) for segment in speech] == [
        ("calm", "slow"),
        ("grounded", "unhurried"),
    ]
    quiet = [segment.duration_ms for segment in compiled.segments if segment.type == "SILENCE"]
    assert quiet == [4321]


def test_delivery_challenger_changes_only_typed_style_and_intensity(
    tmp_path: Path,
) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    source_run_id = uuid4()
    root = tmp_path / str(source_run_id)
    root.mkdir()
    (root / "timeline.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "type": "SPEECH",
                        "text": "Clean words without bracketed cues.",
                        "delivery": "calm",
                        "intensity": "subtle",
                        "pace": "slow",
                        "language": "English",
                        "voice_mode": "preset",
                        "pronunciation_overrides": [],
                    },
                    {"type": "SILENCE", "duration_ms": 7654},
                ]
            }
        ),
        encoding="utf-8",
    )
    condition = _condition().model_copy(
        update={
            "voice": VoiceConfiguration(
                configuration_id="qwen-serena",
                model_id="qwen3-custom-voice-1.7b",
                voice_id="Serena",
                delivery="sleepy",
                intensity="moderate",
            )
        }
    )
    request = ProductionTrialRequest(
        experiment_id="frozen-delivery",
        experiment_version=1,
        experiment_type="delivery",
        controlled_source_run_id=source_run_id,
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="challenger",
        condition=condition,
        seed=42,
    )

    payload = LocalProductionExperimentExecutor(
        cast(Any, SimpleNamespace(runs_directory=tmp_path))
    )._generation_payload(request)
    compiled = build_script_timeline(
        run_id=uuid4(),
        script=cast(str, payload["text"]),
        created_at=datetime.now(UTC),
    )
    speech = [segment for segment in compiled.segments if segment.type == "SPEECH"]

    assert payload["delivery"] == "sleepy"
    assert payload["intensity"] == "moderate"
    assert [(item.text, item.delivery, item.intensity, item.pace) for item in speech] == [
        ("Clean words without bracketed cues.", "sleepy", "moderate", "slow")
    ]
    assert "[delivery" not in speech[0].text
    assert [segment.duration_ms for segment in compiled.segments if segment.type == "SILENCE"] == [
        7654
    ]


def test_progressive_quiet_redistributes_within_target_duration_budget(
    tmp_path: Path,
) -> None:
    (tmp_path / "audio-manifest.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"segment_type": "SPEECH", "actual_duration_ms": 60_000},
                    {"segment_type": "BREATH", "actual_duration_ms": 8_000},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "resolved-config.json").write_text(
        json.dumps({"tts": {"speed": 0.6}}),
        encoding="utf-8",
    )

    durations = LocalProductionExperimentExecutor._progressive_quiet_durations(
        tmp_path,
        source_durations=[20_000, 30_000, 40_000],
        target_seconds=180,
        candidate_speed=0.55,
    )

    expected_speech = round(60_000 * 0.6 / 0.55)
    assert sum(durations) == 180_000 - expected_speech - 8_000
    assert all(duration >= 1_000 for duration in durations)
    assert durations[-1] / 40_000 > durations[0] / 20_000


def test_completed_trial_fingerprints_requested_blueprint(tmp_path: Path) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    run_id = uuid4()
    request = ProductionTrialRequest(
        experiment_id="blueprint-evidence",
        experiment_version=1,
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="candidate",
        condition=_condition(method="plan_first"),
        seed=42,
    )
    root = tmp_path / str(run_id)
    root.mkdir()
    plan = {
        "schema_version": 2,
        "blueprint_id": "grounding@1",
        "blueprint_version": 1,
    }
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (root / "timeline.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
    (root / "audio-manifest.json").write_text(
        json.dumps({"duration_ms": request.case.duration_seconds * 1_000}),
        encoding="utf-8",
    )
    (root / "quality.json").write_text(json.dumps({"clipped_samples": 0}), encoding="utf-8")
    (root / "resolved-config.json").write_text(
        json.dumps(
            {
                "generation": {
                    "plan_prompt_id": "whoopy.plan",
                    "plan_prompt_version": 5,
                    "section_prompt_id": "whoopy.section",
                    "section_prompt_version": 3,
                }
            }
        ),
        encoding="utf-8",
    )
    metadata = EvaluationFixtureGenerator.metadata.model_dump(mode="json")
    (root / "model-metadata.json").write_text(
        json.dumps({"llm": metadata, "tts": metadata}), encoding="utf-8"
    )
    (root / "narration.wav").write_bytes(b"production-audio-evidence")
    application = SimpleNamespace(
        runs_directory=tmp_path,
        environment_status=lambda: {"system": "fixture"},
    )
    executor = LocalProductionExperimentExecutor(cast(Any, application))

    result = executor._completed_result(
        request,
        run_id,
        datetime.now(UTC),
        time.monotonic(),
    )

    assert result.controlled_inputs is not None
    assert result.controlled_inputs.blueprint_sha256 == executor._value_hash(
        {
            "requested": request.case.blueprint_id,
            "category": request.case.category,
        }
    )


def _definition(
    *,
    experiment_type: Literal["llm", "writing_method", "voice", "delivery", "pacing"] = "llm",
) -> ExperimentDefinition:
    first = _condition()
    if experiment_type == "llm":
        second = _condition(llm="qwen3-1.7b")
    elif experiment_type == "writing_method":
        second = _condition(method="three_candidate_structural_selection")
    elif experiment_type == "voice":
        second = _condition(voice="moss-calm")
        second = second.model_copy(
            update={
                "voice": VoiceConfiguration(
                    configuration_id="moss-calm",
                    model_id="moss-5b",
                    voice_id="reference-1",
                    voice_mode="reference",
                    delivery="calm",
                )
            }
        )
    elif experiment_type == "delivery":
        first = first.model_copy(
            update={
                "voice": VoiceConfiguration(
                    configuration_id="qwen-serena",
                    model_id="qwen3-custom-voice-1.7b",
                    voice_id="Serena",
                    delivery="calm",
                )
            }
        )
        second = first.model_copy(
            update={"voice": first.voice.model_copy(update={"delivery": "sleepy"})}
        )
    else:
        second = _condition(pacing="progressive-quiet-v2")
    return ExperimentDefinition(
        experiment_id=f"test-{experiment_type.replace('_', '-')}",
        version=1,
        experiment_type=experiment_type,
        evaluation_id="local-v1-meditation-corpus",
        evaluation_version=1,
        seed=42,
        baseline_candidate_id="baseline",
        candidates=[
            ExperimentCandidate(candidate_id="baseline", label="Baseline", condition=first),
            ExperimentCandidate(candidate_id="candidate", label="Candidate", condition=second),
        ],
    )


class RecordingProductionExecutor:
    def __init__(self, *, metric_value: float = 1.0) -> None:
        self.requests: list[ProductionTrialRequest] = []
        self.metric_value = metric_value

    def execute(self, request: ProductionTrialRequest) -> ProductionTrialResult:
        self.requests.append(request)
        now = datetime.now(UTC)
        canonical_source = (
            CanonicalSourceProvenance(
                source_run_id=request.controlled_source_run_id,
                source_revision_id="original",
                artifact_path=f"runs/{request.controlled_source_run_id}/timeline.json",
                artifact_sha256="7" * 64,
                topology_sha256="8" * 64,
            )
            if request.controlled_source_run_id is not None
            else None
        )
        return ProductionTrialResult(
            trial_id=request.trial_id,
            source_run_id=uuid4(),
            controlled_source_run_id=request.controlled_source_run_id,
            candidate_id=request.candidate_id,
            case_id=request.case.id,
            success=True,
            actual_condition=request.condition,
            started_at=now,
            completed_at=now,
            artifacts=[
                ArtifactReference(
                    artifact_id=f"manifest-{request.candidate_id}",
                    role="manifest",
                    path=f"runs/{request.trial_id}/manifest.json",
                    sha256="a" * 64,
                    media_type="application/json",
                ),
                ArtifactReference(
                    artifact_id=f"audio-{request.trial_id}",
                    role="audio",
                    path=f"runs/{request.trial_id}/meditation.wav",
                    sha256="b" * 64,
                    media_type="audio/wav",
                ),
            ],
            metrics=[
                MetricValue(
                    name="duration_error_percent",
                    value=self.metric_value,
                    unit="percent",
                    goal="lower",
                )
            ],
            adapter_metadata={"llm": EvaluationFixtureGenerator.metadata},
            hardware={"device": "fixture"},
            controlled_inputs=ControlledInputFingerprints(
                prompt_sha256="1" * 64,
                blueprint_sha256="2" * 64,
                generation_schema_sha256="3" * 64,
                spoken_text_sha256="4" * 64,
                speech_controls_sha256="6" * 64,
                silence_timeline_sha256="5" * 64,
                timeline_topology_sha256="8" * 64,
                pacing_profile_sha256="9" * 64,
            ),
            canonical_source=canonical_source,
        )


@pytest.mark.parametrize(
    "experiment_type", ["llm", "writing_method", "voice", "delivery", "pacing"]
)
def test_all_lab_experiment_types_enforce_one_variable(
    experiment_type: Literal["llm", "writing_method", "voice", "delivery", "pacing"],
) -> None:
    definition = _definition(experiment_type=experiment_type)

    assert definition.experiment_type == experiment_type

    extra_change = (
        {"pacing_profile_id": "also-changed"}
        if experiment_type == "llm"
        else {"llm_model_id": "also-changed"}
    )
    changed_twice = definition.candidates[1].condition.model_copy(update=extra_change)
    with pytest.raises(ValidationError, match="may only change"):
        definition.model_copy(
            update={
                "candidates": [
                    definition.candidates[0],
                    definition.candidates[1].model_copy(update={"condition": changed_twice}),
                ]
            }
        ).model_validate(
            definition.model_copy(
                update={
                    "candidates": [
                        definition.candidates[0],
                        definition.candidates[1].model_copy(update={"condition": changed_twice}),
                    ]
                }
            ).model_dump()
        )


def test_lab_rejects_pacing_experiment_for_adapter_without_speed_control() -> None:
    definition = _definition(experiment_type="pacing")
    moss_voice = VoiceConfiguration(
        configuration_id="moss-reference",
        model_id="moss-5b",
        voice_id="reference-1",
        voice_mode="reference",
    )
    definition = definition.model_copy(
        update={
            "candidates": [
                candidate.model_copy(
                    update={
                        "condition": candidate.condition.model_copy(update={"voice": moss_voice})
                    }
                )
                for candidate in definition.candidates
            ]
        }
    )

    with pytest.raises(EvaluationExecutionError, match="currently require Kokoro"):
        LocalProductionExperimentExecutor.validate_definition(definition)


def test_voice_axis_keeps_delivery_fixed_and_delivery_axis_accepts_typed_route() -> None:
    definition = _definition(experiment_type="voice")
    challenger = definition.candidates[1]
    changed_voice = challenger.condition.voice.model_copy(update={"delivery": "sleepy"})
    definition = definition.model_copy(
        update={
            "candidates": [
                definition.candidates[0],
                challenger.model_copy(
                    update={
                        "condition": challenger.condition.model_copy(
                            update={"voice": changed_voice}
                        )
                    }
                ),
            ]
        }
    )

    with pytest.raises(EvaluationExecutionError, match="delivery-preset comparison"):
        LocalProductionExperimentExecutor.validate_definition(definition)

    delivery_definition = definition.model_copy(update={"experiment_type": "delivery"})
    LocalProductionExperimentExecutor.validate_definition(
        delivery_definition.model_copy(
            update={
                "candidates": [
                    candidate.model_copy(
                        update={
                            "condition": candidate.condition.model_copy(
                                update={
                                    "voice": candidate.condition.voice.model_copy(
                                        update={
                                            "model_id": "qwen3-custom-voice-0.6b",
                                            "voice_id": "Serena",
                                            "configuration_id": "qwen-serena",
                                        }
                                    )
                                }
                            )
                        }
                    )
                    for candidate in delivery_definition.candidates
                ]
            }
        )
    )


def test_delivery_axis_rejects_voice_identity_change_and_non_expressive_adapter() -> None:
    first = _condition().model_copy(
        update={
            "voice": VoiceConfiguration(
                configuration_id="qwen-serena",
                model_id="qwen3-custom-voice-1.7b",
                voice_id="Serena",
                delivery="calm",
            )
        }
    )
    second = first.model_copy(
        update={"voice": first.voice.model_copy(update={"delivery": "sleepy"})}
    )
    definition = _definition(experiment_type="delivery").model_copy(
        update={
            "candidates": [
                ExperimentCandidate(candidate_id="baseline", label="Calm", condition=first),
                ExperimentCandidate(candidate_id="candidate", label="Sleepy", condition=second),
            ]
        }
    )
    definition = ExperimentDefinition.model_validate(definition.model_dump())
    LocalProductionExperimentExecutor.validate_definition(definition)

    changed_identity = second.model_copy(
        update={"voice": second.voice.model_copy(update={"voice_id": "Vivian"})}
    )
    with pytest.raises(ValidationError, match=r"may only change voice\.delivery"):
        ExperimentDefinition.model_validate(
            definition.model_copy(
                update={
                    "candidates": [
                        definition.candidates[0],
                        definition.candidates[1].model_copy(update={"condition": changed_identity}),
                    ]
                }
            ).model_dump()
        )

    kokoro = definition.model_copy(
        update={
            "candidates": [
                candidate.model_copy(
                    update={
                        "condition": candidate.condition.model_copy(
                            update={
                                "voice": candidate.condition.voice.model_copy(
                                    update={
                                        "model_id": "kokoro",
                                        "voice_id": "af_heart",
                                        "configuration_id": "kokoro-heart",
                                    }
                                )
                            }
                        )
                    }
                )
                for candidate in definition.candidates
            ]
        }
    )
    with pytest.raises(EvaluationExecutionError, match="expressive"):
        LocalProductionExperimentExecutor.validate_definition(kokoro)


def test_lab_attests_durable_candidate_controls_instead_of_echoing_request() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    request = ProductionTrialRequest(
        experiment_id="attested-controls",
        experiment_version=1,
        experiment_type="pacing",
        controlled_source_run_id=uuid4(),
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="candidate",
        condition=_condition(pacing="progressive-quiet-v2"),
        seed=42,
    )
    resolved = {
        "mode": "script_file",
        "profile": "basic",
        "tts": {
            "backend": "kokoro",
            "voice_name": "af_heart",
            "speed": 0.6,
            "language": "en-us",
            "seed": 42,
            "delivery_override": "calm",
            "voice_mode": "preset",
        },
    }
    source_resolved = {
        "generation": {
            "seed": 42,
            "writing_method": "blueprint_first_sparse",
            "duration_seconds": request.case.duration_seconds,
            "meditation_kind": request.case.category,
        }
    }
    metadata = {
        "llm": {
            "versioned_model_id": "Qwen3 4B Q4_K_M@reviewed",
        }
    }

    with pytest.raises(ValueError, match=r"tts\.speed=0\.6.*expected 0\.55"):
        LocalProductionExperimentExecutor(cast(Any, object()))._attest_candidate_controls(
            request,
            resolved=resolved,
            model_metadata=metadata,
            source_resolved=source_resolved,
            source_prompt=request.case.prompt,
            source_plan={"blueprint_id": request.case.blueprint_id},
        )


@pytest.mark.parametrize("experiment_type", ["voice", "delivery", "pacing"])
def test_completed_controlled_candidate_rejects_duration_over_five_percent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    experiment_type: str,
) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    source_run_id = uuid4()
    run_id = uuid4()
    source = tmp_path / str(source_run_id)
    root = tmp_path / str(run_id)
    source.mkdir()
    root.mkdir()
    request = ProductionTrialRequest(
        experiment_id="duration-gate",
        experiment_version=1,
        experiment_type=cast(Any, experiment_type),
        controlled_source_run_id=source_run_id,
        trial_id=uuid4(),
        case=evaluation.cases[0],
        candidate_id="candidate",
        condition=_condition(pacing="progressive-quiet-v2"),
        seed=42,
    )
    (source / "plan.json").write_text(
        json.dumps({"schema_version": 2, "blueprint_id": "grounding@1"}),
        encoding="utf-8",
    )
    (source / "resolved-config.json").write_text(
        json.dumps({"generation": {"seed": 42}}), encoding="utf-8"
    )
    (source / "model-metadata.json").write_text(
        json.dumps({"llm": EvaluationFixtureGenerator.metadata.model_dump(mode="json")}),
        encoding="utf-8",
    )
    (source / "run.json").write_text(json.dumps({"prompt": request.case.prompt}), encoding="utf-8")
    timeline = {
        "segments": [
            {
                "type": "SPEECH",
                "text": "Stay here.",
                "delivery": "calm",
                "intensity": "subtle",
                "language": "English",
                "voice_mode": "preset",
                "pace": "slow",
                "pronunciation_overrides": [],
            },
            {"type": "SILENCE", "duration_ms": 1000},
        ]
    }
    (root / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    (source / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    (root / "audio-manifest.json").write_text(
        json.dumps({"duration_ms": request.case.duration_seconds * 1_100}),
        encoding="utf-8",
    )
    (root / "quality.json").write_text(json.dumps({"clipped_samples": 0}), encoding="utf-8")
    (root / "resolved-config.json").write_text(
        json.dumps({"mode": "script_file", "tts": {}}), encoding="utf-8"
    )
    metadata = EvaluationFixtureGenerator.metadata.model_dump(mode="json")
    (root / "model-metadata.json").write_text(
        json.dumps({"tts": metadata, "llm": None}), encoding="utf-8"
    )
    (root / "narration.wav").write_bytes(b"production-audio-evidence")
    application = SimpleNamespace(
        runs_directory=tmp_path,
        environment_status=lambda: {"system": "fixture"},
    )
    executor = LocalProductionExperimentExecutor(cast(Any, application))
    monkeypatch.setattr(executor, "_attest_candidate_controls", lambda *args, **kwargs: None)

    with pytest.raises(ValueError, match="outside the requested duration gate"):
        executor._completed_result(request, run_id, datetime.now(UTC), time.monotonic())


def test_lab_runs_single_case_sequentially_and_builds_deterministic_blind_pair() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    definition = _definition()
    executor = RecordingProductionExecutor()
    lab = EvaluationLab(evaluation_set=evaluation, platform="test-x86_64")

    first = lab.run(definition, executor, case_ids=["grounding-3m"])
    second = lab.run(definition, RecordingProductionExecutor(), case_ids=["grounding-3m"])

    assert [request.candidate_id for request in executor.requests] == ["baseline", "candidate"]
    assert len(first.trials) == 2
    assert len(first.blind_assignments) == 1
    assert first.blind_assignments == second.blind_assignments
    candidates_by_side = first.blind_assignments[0].sealed_candidate_by_side
    assert set(candidates_by_side.values()) == {
        "baseline",
        "candidate",
    }
    reviewer_payload = [item.model_dump(mode="json") for item in blind_presentations(first)]
    assert "candidate" not in json.dumps(reviewer_payload)
    media_token = blind_presentations(first)[0].left_media_token
    assert resolve_blind_audio(first, media_token).media_type == "audio/wav"


def test_lab_halt_preserves_completed_trials_for_explicit_resume() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    definition = _definition()
    executor = RecordingProductionExecutor()
    halt = False

    def trial_completed(_trials: tuple[ProductionTrialResult, ...]) -> None:
        nonlocal halt
        halt = True

    with pytest.raises(EvaluationExecutionHalted) as stopped:
        EvaluationLab(evaluation_set=evaluation, platform="test").run(
            definition,
            executor,
            case_ids=["grounding-3m"],
            should_halt=lambda: halt,
            trial_completed=trial_completed,
        )

    resumed_executor = RecordingProductionExecutor()
    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        definition,
        resumed_executor,
        case_ids=["grounding-3m"],
        existing_trials=stopped.value.trials,
    )

    assert len(stopped.value.trials) == 1
    assert [request.candidate_id for request in resumed_executor.requests] == ["candidate"]
    assert len(report.trials) == 2


def test_controlled_comparison_passes_baseline_provenance_to_challenger() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    executor = RecordingProductionExecutor()
    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        _definition(experiment_type="pacing"),
        executor,
        case_ids=["grounding-3m"],
    )

    assert executor.requests[0].controlled_source_run_id is None
    assert executor.requests[1].controlled_source_run_id == report.trials[0].source_run_id
    assert executor.requests[1].experiment_type == "pacing"


def test_controlled_comparison_does_not_use_challenger_when_baseline_fails() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))

    class FailedBaselineExecutor(RecordingProductionExecutor):
        def execute(self, request: ProductionTrialRequest) -> ProductionTrialResult:
            result = super().execute(request)
            return result.model_copy(
                update={
                    "success": False,
                    "failure": "baseline failed",
                    "artifacts": [],
                    "metrics": [],
                    "adapter_metadata": {},
                    "hardware": {},
                    "controlled_inputs": None,
                }
            )

    executor = FailedBaselineExecutor()
    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        _definition(experiment_type="voice"),
        executor,
        case_ids=["grounding-3m"],
    )

    assert [request.candidate_id for request in executor.requests] == ["baseline"]
    assert len(report.trials) == 1
    assert not report.trials[0].success
    assert report.blind_assignments == []


def test_lab_rejects_production_evidence_for_different_controls() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    definition = _definition()

    class LyingExecutor(RecordingProductionExecutor):
        def execute(self, request: ProductionTrialRequest) -> ProductionTrialResult:
            result = super().execute(request)
            return result.model_copy(update={"actual_condition": _condition(pacing="changed")})

    with pytest.raises(EvaluationExecutionError, match="actual_condition"):
        EvaluationLab(evaluation_set=evaluation, platform="test").run(
            definition,
            LyingExecutor(),
            case_ids=["grounding-3m"],
        )


def test_voice_comparison_rejects_changed_spoken_text() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    definition = _definition(experiment_type="voice")

    class DriftedTextExecutor(RecordingProductionExecutor):
        def execute(self, request: ProductionTrialRequest) -> ProductionTrialResult:
            result = super().execute(request)
            if request.candidate_id == "candidate":
                assert result.controlled_inputs is not None
                changed = result.controlled_inputs.model_copy(
                    update={"spoken_text_sha256": "9" * 64}
                )
                return result.model_copy(update={"controlled_inputs": changed})
            return result

    with pytest.raises(EvaluationExecutionError, match="spoken_text_sha256"):
        EvaluationLab(evaluation_set=evaluation, platform="test").run(
            definition,
            DriftedTextExecutor(),
            case_ids=["grounding-3m"],
        )


def test_pacing_comparison_rejects_changed_typed_speech_controls() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    definition = _definition(experiment_type="pacing")

    class DriftedDeliveryExecutor(RecordingProductionExecutor):
        def execute(self, request: ProductionTrialRequest) -> ProductionTrialResult:
            result = super().execute(request)
            if request.candidate_id == "candidate":
                assert result.controlled_inputs is not None
                changed = result.controlled_inputs.model_copy(
                    update={"speech_controls_sha256": "9" * 64}
                )
                return result.model_copy(update={"controlled_inputs": changed})
            return result

    with pytest.raises(EvaluationExecutionError, match="speech_controls_sha256"):
        EvaluationLab(evaluation_set=evaluation, platform="test").run(
            definition,
            DriftedDeliveryExecutor(),
            case_ids=["grounding-3m"],
        )


def test_delivery_comparison_allows_typed_speech_control_fingerprint_to_change() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))

    class DeliveryExecutor(RecordingProductionExecutor):
        def execute(self, request: ProductionTrialRequest) -> ProductionTrialResult:
            result = super().execute(request)
            assert result.controlled_inputs is not None
            changed = result.controlled_inputs.model_copy(
                update={
                    "speech_controls_sha256": (
                        "6" * 64 if request.candidate_id == "baseline" else "9" * 64
                    )
                }
            )
            return result.model_copy(update={"controlled_inputs": changed})

    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        _definition(experiment_type="delivery"),
        DeliveryExecutor(),
        case_ids=["grounding-3m"],
    )

    assert len(report.trials) == 2
    assert len(report.blind_assignments) == 1
    assert {
        trial.controlled_inputs.speech_controls_sha256
        for trial in report.trials
        if trial.controlled_inputs is not None
    } == {"6" * 64, "9" * 64}


def test_ratings_regressions_promotion_and_immutable_storage(tmp_path: Path) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    definition = _definition()
    lab = EvaluationLab(evaluation_set=evaluation, platform="test")
    baseline = lab.run(
        definition,
        RecordingProductionExecutor(metric_value=2.0),
        case_ids=["grounding-3m"],
    )
    report = lab.run(
        definition,
        RecordingProductionExecutor(metric_value=1.0),
        case_ids=["grounding-3m"],
    )
    content_rating = HumanRating(
        rating_id=uuid4(),
        assignment_id=report.blind_assignments[0].assignment_id,
        dimension="content",
        score=5,
        preferred_side="right",
        notes="The quieter delivery left more room for practice.",
        created_at=datetime.now(UTC),
    )
    voice_rating = HumanRating(
        rating_id=uuid4(),
        assignment_id=report.blind_assignments[0].assignment_id,
        dimension="voice",
        score=4,
        preferred_side="right",
        notes="The voice remained comfortable.",
        created_at=datetime.now(UTC),
    )
    report = add_rating(add_rating(report, content_rating), voice_rating)
    regressions = compare_against_baseline(
        report,
        baseline,
        candidate_id="candidate",
        baseline_candidate_id="baseline",
    )
    report = report.model_copy(update={"regressions": regressions})
    with pytest.raises(EvaluationExecutionError, match="must identify"):
        create_promotion_decision(
            report,
            outcome="promote",
            candidate_id="candidate",
            previous_value="qwen3-4b",
            selected_value="qwen3-1.7b",
            rationale="A role-less model promotion must not become a default.",
            report_sha256="b" * 64,
        )
    decision = create_promotion_decision(
        report,
        outcome="promote",
        candidate_id="candidate",
        previous_value="qwen3-4b",
        selected_value="qwen3-1.7b",
        rationale="Blind review preferred the candidate and duration error improved.",
        report_sha256="b" * 64,
        selection_role="low_resource_fallback",
        supported_low_resource_llm_ids=("qwen3-1.7b", "qwen3-1.7b-lite"),
    )
    repository = EvaluationRepository(tmp_path)
    path = repository.save_report(report)
    repository.save_decision(decision)

    assert regressions[0].direction == "improved"
    assert repository.load_report(experiment_id=definition.experiment_id, version=1) == report
    assert (
        repository.report_sha256(experiment_id=definition.experiment_id, version=1)
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert repository.decisions() == [decision]
    assert path.is_file()
    with pytest.raises(Exception, match="Refusing to replace"):
        repository.save_report(report)


def test_blind_review_requires_typed_dimensions_preference_and_full_set() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        _definition(),
        RecordingProductionExecutor(),
        case_ids=["grounding-3m"],
    )
    assignment = report.blind_assignments[0]
    content = HumanRating(
        rating_id=uuid4(),
        assignment_id=assignment.assignment_id,
        dimension="content",
        score=4,
        preferred_side="right",
        notes="The preferred sample gave clearer guidance.",
        created_at=datetime.now(UTC),
    )
    report = add_rating(report, content)
    partial = blind_review_status(
        report,
        expected_case_ids=(case.id for case in evaluation.cases),
    )

    assert partial["complete_current_pairs"] is False
    assert partial["complete_evaluation_set"] is False
    assert partial["missing_dimensions_by_assignment"] == {str(assignment.assignment_id): ["voice"]}
    assert len(partial["missing_case_ids"]) == 9
    with pytest.raises(EvaluationExecutionError, match="complete versioned evaluation set"):
        create_promotion_decision(
            report,
            outcome="promote",
            candidate_id="candidate",
            previous_value="qwen3-4b",
            selected_value="qwen3-1.7b",
            rationale="One case must remain a trial, not a product decision.",
            report_sha256="b" * 64,
            selection_role="low_resource_fallback",
            expected_case_ids=(case.id for case in evaluation.cases),
        )


def test_duplicate_dimension_rating_is_rejected() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        _definition(),
        RecordingProductionExecutor(),
        case_ids=["grounding-3m"],
    )
    assignment_id = report.blind_assignments[0].assignment_id
    rating = HumanRating(
        rating_id=uuid4(),
        assignment_id=assignment_id,
        dimension="content",
        score=4,
        preferred_side="right",
        created_at=datetime.now(UTC),
    )
    report = add_rating(report, rating)
    with pytest.raises(EvaluationExecutionError, match="content rating already exists"):
        add_rating(report, rating.model_copy(update={"rating_id": uuid4()}))


def test_complete_ten_case_human_review_can_create_role_bound_decision() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        _definition(),
        RecordingProductionExecutor(),
    )
    for assignment in report.blind_assignments:
        preferred_side = next(
            side
            for side, candidate_id in assignment.sealed_candidate_by_side.items()
            if candidate_id == "candidate"
        )
        for dimension in ("content", "voice"):
            report = add_rating(
                report,
                HumanRating(
                    rating_id=uuid4(),
                    assignment_id=assignment.assignment_id,
                    dimension=dimension,
                    score=4,
                    preferred_side=preferred_side,
                    notes="Reviewed blind against the fixed baseline.",
                    created_at=datetime.now(UTC),
                ),
            )

    status = blind_review_status(
        report,
        expected_case_ids=(case.id for case in evaluation.cases),
    )
    decision = create_promotion_decision(
        report,
        outcome="promote",
        candidate_id="candidate",
        previous_value="qwen3-4b",
        selected_value="qwen3-1.7b",
        rationale="Every case was reviewed and the challenger was preferred.",
        report_sha256="c" * 64,
        selection_role="low_resource_fallback",
        expected_case_ids=(case.id for case in evaluation.cases),
        supported_low_resource_llm_ids=("qwen3-1.7b", "qwen3-1.7b-lite"),
    )

    assert len(report.blind_assignments) == 10
    assert status["promotion_ready"] is True
    assert decision.selection_role == "low_resource_fallback"
    assert len(decision.rating_ids) == 20
    assert decision.schema_version == 3
    assert set(decision.rating_sha256_by_id or {}) == {
        str(rating.rating_id) for rating in report.ratings
    }


def test_promotion_rejects_losing_candidate_and_weak_human_scores() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        _definition(),
        RecordingProductionExecutor(),
    )
    for assignment in report.blind_assignments:
        baseline_side = next(
            side
            for side, candidate_id in assignment.sealed_candidate_by_side.items()
            if candidate_id == "baseline"
        )
        for dimension in ("content", "voice"):
            report = add_rating(
                report,
                HumanRating(
                    rating_id=uuid4(),
                    assignment_id=assignment.assignment_id,
                    dimension=dimension,
                    score=4,
                    preferred_side=baseline_side,
                    created_at=datetime.now(UTC),
                ),
            )

    with pytest.raises(EvaluationExecutionError, match="win more blind preferences"):
        create_promotion_decision(
            report,
            outcome="promote",
            candidate_id="candidate",
            previous_value="qwen3-4b",
            selected_value="qwen3-1.7b",
            rationale="This candidate actually lost the blind review.",
            report_sha256="a" * 64,
            selection_role="portable_default",
            expected_case_ids=(case.id for case in evaluation.cases),
        )

    fallback = create_promotion_decision(
        report,
        outcome="promote",
        candidate_id="candidate",
        previous_value="qwen3-4b",
        selected_value="qwen3-1.7b",
        rationale="The Lite candidate remains acceptable as the lower-resource fallback.",
        report_sha256="a" * 64,
        selection_role="low_resource_fallback",
        expected_case_ids=(case.id for case in evaluation.cases),
        supported_low_resource_llm_ids=("qwen3-1.7b", "qwen3-1.7b-lite"),
    )
    assert fallback.selection_role == "low_resource_fallback"

    with pytest.raises(EvaluationExecutionError, match="production Lite profile"):
        create_promotion_decision(
            report,
            outcome="promote",
            candidate_id="baseline",
            previous_value="qwen3-1.7b",
            selected_value="qwen3-4b",
            rationale="A Standard model must not be mislabeled as the fallback.",
            report_sha256="a" * 64,
            selection_role="low_resource_fallback",
            expected_case_ids=(case.id for case in evaluation.cases),
            supported_low_resource_llm_ids=("qwen3-1.7b", "qwen3-1.7b-lite"),
        )

    weak = report.model_copy(
        update={
            "ratings": [
                rating.model_copy(update={"score": 2}) if index == 0 else rating
                for index, rating in enumerate(report.ratings)
            ]
        }
    )
    with pytest.raises(EvaluationExecutionError, match="every required human rating"):
        create_promotion_decision(
            weak,
            outcome="promote",
            candidate_id="baseline",
            previous_value="qwen3-1.7b",
            selected_value="qwen3-4b",
            rationale="A low required score must fail closed.",
            report_sha256="a" * 64,
            selection_role="portable_default",
            expected_case_ids=(case.id for case in evaluation.cases),
        )


def test_report_rejects_forged_blind_seal_and_duplicate_trial_keys() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = EvaluationLab(evaluation_set=evaluation, platform="test").run(
        _definition(), RecordingProductionExecutor(), case_ids=["grounding-3m"]
    )
    payload = report.model_dump(mode="json")
    payload["blind_assignments"][0]["sealed_candidate_by_side"] = {
        "left": "baseline",
        "right": "baseline",
    }
    with pytest.raises(ValidationError, match="seal must match"):
        type(report).model_validate(payload)

    payload = report.model_dump(mode="json")
    payload["trials"].append(payload["trials"][0])
    with pytest.raises(ValidationError, match="trial IDs must be unique"):
        type(report).model_validate(payload)


def test_repository_rating_identity_is_assignment_dimension_and_paths_are_validated(
    tmp_path: Path,
) -> None:
    repository = EvaluationRepository(tmp_path)
    assignment_id = uuid4()
    first = HumanRating(
        rating_id=uuid4(),
        assignment_id=assignment_id,
        dimension="voice",
        score=4,
        created_at=datetime.now(UTC),
    )
    second = first.model_copy(update={"rating_id": uuid4(), "score": 5})
    repository.save_rating(experiment_id="safe-experiment", experiment_version=1, rating=first)
    with pytest.raises(Exception, match="Refusing to replace immutable Lab evidence"):
        repository.save_rating(experiment_id="safe-experiment", experiment_version=1, rating=second)
    with pytest.raises(Exception, match="Invalid Lab experiment ID"):
        repository.load_report(experiment_id="../../escape", version=1)


def test_lab_waits_for_worker_lock_before_hashing_final_telemetry(tmp_path: Path) -> None:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    executor = LocalProductionExperimentExecutor(
        application,
        poll_seconds=0.005,
        timeout_seconds=2,
    )
    run_id = uuid4()
    run_root = application.runs_directory / str(run_id)
    run_root.mkdir(parents=True)
    metadata = run_root / "model-metadata.json"
    metadata.write_text('{"telemetry": null}\n', encoding="utf-8")
    worker_lock = RunLock(run_root)
    worker_lock.acquire()

    def finish_worker() -> None:
        time.sleep(0.03)
        metadata.write_text('{"telemetry": {"captured": true}}\n', encoding="utf-8")
        worker_lock.release()

    worker = threading.Thread(target=finish_worker)
    worker.start()
    evidence_lock = executor._acquire_finalized_run_lock(run_id, time.monotonic())
    try:
        assert json.loads(metadata.read_text("utf-8"))["telemetry"] == {"captured": True}
    finally:
        evidence_lock.release()
        worker.join(timeout=1)
    assert not worker.is_alive()


def test_legacy_roleless_model_decision_remains_readable_but_new_promotions_require_role() -> None:
    legacy = PromotionDecision(
        schema_version=1,
        decision_id=uuid4(),
        created_at=datetime.now(UTC),
        experiment_id="legacy-llm",
        experiment_version=1,
        outcome="promote",
        studio_target="llm",
        candidate_id="qwen3-4b",
        previous_value="old",
        selected_value="Qwen3 4B",
        rationale="Historical record before explicit model roles.",
        report_sha256="a" * 64,
        rating_ids=[uuid4()],
    )

    assert legacy.selection_role is None
    with pytest.raises(ValidationError, match="require a model selection role"):
        PromotionDecision.model_validate({**legacy.model_dump(mode="json"), "schema_version": 2})


@pytest.mark.parametrize(
    ("baseline_update", "message"),
    [
        ({"platform": "different-hardware"}, "same hardware platform"),
        (
            {"definition": _definition().model_copy(update={"evaluation_version": 2})},
            "same versioned evaluation set",
        ),
        (
            {"definition": _definition(experiment_type="voice")},
            "same experiment type",
        ),
    ],
)
def test_regression_comparison_rejects_unmatched_baseline_evidence(
    baseline_update: dict[str, object],
    message: str,
) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = EvaluationLab(evaluation_set=evaluation, platform="same-hardware").run(
        _definition(),
        RecordingProductionExecutor(),
        case_ids=["grounding-3m"],
    )
    baseline = report.model_copy(update=baseline_update)

    with pytest.raises(EvaluationExecutionError, match=message):
        compare_against_baseline(
            report,
            baseline,
            candidate_id="candidate",
            baseline_candidate_id="baseline",
        )


def test_sequential_queue_never_starts_two_experiments() -> None:
    queue = SequentialExperimentQueue()
    first = _definition()
    second = first.model_copy(update={"experiment_id": "test-second"})
    queue.enqueue(first)
    queue.enqueue(second)

    assert queue.start_next() is not None
    assert queue.start_next() is None
    queue.finish(experiment_id=first.experiment_id, experiment_version=first.version)
    running = queue.start_next()

    assert running is not None
    assert running.experiment_id == second.experiment_id


def test_automatic_combined_score_is_forbidden() -> None:
    with pytest.raises(ValidationError, match="combined scores"):
        MetricValue(name="combined_score", value=4.2, unit="points", goal="higher")
