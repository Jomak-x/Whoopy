from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from whoopy.evaluation import (
    ArtifactReference,
    BlindAssignment,
    ControlledInputFingerprints,
    EvaluationCondition,
    ExperimentCandidate,
    ExperimentDefinition,
    ExperimentReport,
    HumanRating,
    MetricValue,
    ProductionTrialResult,
    VoiceConfiguration,
    assess_corpus_acceptance,
    load_evaluation_set,
)
from whoopy.ports import AdapterMetadata
from whoopy.release.status import assess_lab


def _report(
    *,
    safety_value: float = 1,
    rating_score: int = 4,
    preferred_side: Literal["left", "right", "tie"] | None = "right",
) -> ExperimentReport:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    baseline_voice = VoiceConfiguration(
        configuration_id="baseline",
        model_id="kokoro@1",
        voice_id="af_heart",
    )
    condition = EvaluationCondition(
        llm_model_id="qwen3-4b",
        writing_method="blueprint_first_sparse",
        voice=baseline_voice,
        pacing_profile_id="meditative-v2",
    )
    definition = ExperimentDefinition(
        experiment_id="local-v1-acceptance-fixture",
        version=1,
        experiment_type="writing_method",
        evaluation_id=evaluation.evaluation_id,
        evaluation_version=evaluation.version,
        seed=42,
        candidates=[
            ExperimentCandidate(candidate_id="baseline", label="Baseline", condition=condition),
            ExperimentCandidate(
                candidate_id="candidate",
                label="Candidate",
                condition=condition.model_copy(
                    update={"writing_method": "three_candidate_structural_selection"}
                ),
            ),
        ],
        baseline_candidate_id="baseline",
    )
    now = datetime.now(UTC)
    metadata = AdapterMetadata(
        adapter_id="test",
        versioned_model_id="fixture@1",
        runtime_id="test",
        runtime_version="1",
        license_id="CC0-1.0",
        device="cpu",
    )
    fingerprints = ControlledInputFingerprints(
        prompt_sha256="a" * 64,
        blueprint_sha256="b" * 64,
        generation_schema_sha256="c" * 64,
        spoken_text_sha256="d" * 64,
        speech_controls_sha256="f" * 64,
        silence_timeline_sha256="e" * 64,
    )
    trials: list[ProductionTrialResult] = []
    assignments: list[BlindAssignment] = []
    ratings: list[HumanRating] = []
    for case in evaluation.cases:
        trial_ids = []
        for candidate in definition.candidates:
            trial_id = uuid5(NAMESPACE_URL, f"{case.id}:{candidate.candidate_id}")
            trial_ids.append(trial_id)
            trials.append(
                ProductionTrialResult(
                    trial_id=trial_id,
                    source_run_id=uuid4(),
                    candidate_id=candidate.candidate_id,
                    case_id=case.id,
                    success=True,
                    actual_condition=candidate.condition,
                    started_at=now,
                    completed_at=now,
                    artifacts=[
                        ArtifactReference(
                            artifact_id=f"manifest-{case.id}-{candidate.candidate_id}",
                            role="manifest",
                            path="manifest.json",
                            sha256="f" * 64,
                            media_type="application/json",
                        ),
                        ArtifactReference(
                            artifact_id=f"audio-{case.id}-{candidate.candidate_id}",
                            role="audio",
                            path="audio.wav",
                            sha256="0" * 64,
                            media_type="audio/wav",
                        ),
                    ],
                    metrics=[
                        MetricValue(
                            name="safety_validation_passed",
                            value=safety_value,
                            unit="boolean",
                            goal="higher",
                        )
                    ],
                    adapter_metadata={"tts": metadata},
                    hardware={"system": "test"},
                    controlled_inputs=fingerprints,
                )
            )
        assignment_id = uuid5(NAMESPACE_URL, f"assignment:{case.id}")
        assignments.append(
            BlindAssignment(
                assignment_id=assignment_id,
                case_id=case.id,
                left_trial_id=trial_ids[0],
                right_trial_id=trial_ids[1],
                sealed_candidate_by_side={"left": "baseline", "right": "candidate"},
            )
        )
        for dimension in ("content", "voice"):
            ratings.append(
                HumanRating(
                    rating_id=uuid4(),
                    assignment_id=assignment_id,
                    dimension=dimension,
                    score=rating_score,
                    preferred_side=preferred_side,
                    created_at=now,
                )
            )
    return ExperimentReport(
        definition=definition,
        created_at=now,
        platform="test",
        trials=trials,
        blind_assignments=assignments,
        ratings=ratings,
        comparison_status="complete",
        incomplete_candidates_by_case={},
    )


def _write_release_report(
    lab_root: Path,
    report: ExperimentReport,
    *,
    version: int,
) -> Path:
    artifact_root = lab_root.parent / f"release-run-v{version}"
    artifact_root.mkdir(parents=True)
    manifest = artifact_root / "manifest.json"
    audio = artifact_root / "audio.wav"
    manifest.write_bytes(f"manifest-v{version}".encode())
    audio.write_bytes(f"audio-v{version}".encode())
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    audio_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
    trials = []
    for trial in report.trials:
        trials.append(
            trial.model_copy(
                update={
                    "artifacts": [
                        ArtifactReference(
                            artifact_id=f"manifest-{trial.trial_id}",
                            role="manifest",
                            path=str(manifest),
                            sha256=manifest_hash,
                            media_type="application/json",
                        ),
                        ArtifactReference(
                            artifact_id=f"audio-{trial.trial_id}",
                            role="audio",
                            path=str(audio),
                            sha256=audio_hash,
                            media_type="audio/wav",
                        ),
                    ]
                }
            )
        )
    revision = report.model_copy(
        update={
            "definition": report.definition.model_copy(update={"version": version}),
            "trials": trials,
            "ratings": [],
        }
    )
    report_path = (
        lab_root / "experiments" / revision.definition.experiment_id / f"v{version}" / "report.json"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text(revision.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def test_release_gate_uses_latest_immutable_experiment_revision_without_fallback(
    tmp_path: Path,
) -> None:
    lab_root = tmp_path / "runs" / ".whoopy-lab"
    report = _report()
    old_manifest = _write_release_report(lab_root, report, version=1)
    current_manifest = _write_release_report(lab_root, report, version=2)
    old_manifest.write_bytes(b"superseded source run changed")

    comparisons, _decisions = assess_lab(lab_root)

    complete_types = comparisons.facts["complete_comparison_types"]
    assert isinstance(complete_types, list)
    assert "writing_method" in complete_types
    assert not any("/v1/report.json" in blocker for blocker in comparisons.blockers)

    current_manifest.write_bytes(b"current source run changed")
    comparisons, _decisions = assess_lab(lab_root)

    complete_types = comparisons.facts["complete_comparison_types"]
    assert isinstance(complete_types, list)
    assert "writing_method" not in complete_types
    assert any("/v2/report.json" in blocker for blocker in comparisons.blockers)


def test_missing_human_and_automatic_evidence_stays_explicitly_blocked() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))

    summary = assess_corpus_acceptance(evaluation, [])

    assert summary.accepted is False
    assert summary.covered_case_count == 0
    assert summary.content_median is None
    assert summary.voice_median is None
    assert summary.no_safety_failures is False
    assert summary.complete_human_coverage is False
    assert summary.complete_baseline_content_comparison is False
    assert summary.content_improves_over_baseline is False
    assert len(summary.cases) == 10
    assert all(case.missing_human_dimensions == ["content", "voice"] for case in summary.cases)


def test_complete_ten_case_production_and_human_evidence_passes() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = _report()

    summary = assess_corpus_acceptance(evaluation, [(report, [])])

    assert summary.accepted is True
    assert summary.covered_case_count == 10
    assert summary.no_safety_failures is True
    assert summary.content_median == 4
    assert summary.voice_median == 4
    assert summary.lowest_required_human_score == 4
    assert summary.complete_baseline_content_comparison is True
    assert summary.challenger_content_preference_count == 10
    assert summary.baseline_content_preference_count == 0
    assert summary.content_improves_over_baseline is True
    assert summary.blockers == []


def test_safety_failure_and_low_human_dimension_each_block_acceptance() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = _report(safety_value=0, rating_score=2)

    summary = assess_corpus_acceptance(evaluation, [(report, [])])

    assert summary.accepted is False
    assert summary.no_safety_failures is False
    assert summary.lowest_required_human_score == 2
    assert any("Safety failures" in blocker for blocker in summary.blockers)
    assert any("every content/voice rating" in blocker for blocker in summary.blockers)


def test_separately_stored_ratings_count_without_mutating_report() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    report = _report()
    stored = report.ratings
    report_without_embedded_ratings = report.model_copy(update={"ratings": []})

    summary = assess_corpus_acceptance(
        evaluation,
        [(report_without_embedded_ratings, stored)],
    )

    assert summary.accepted is True


def test_high_scores_without_blind_improvement_over_baseline_do_not_pass() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    baseline_preferred = _report(preferred_side="left")

    summary = assess_corpus_acceptance(evaluation, [(baseline_preferred, [])])

    assert summary.content_median == 4
    assert summary.voice_median == 4
    assert summary.complete_baseline_content_comparison is True
    assert summary.content_improves_over_baseline is False
    assert summary.accepted is False
    assert any("does not improve over baseline" in blocker for blocker in summary.blockers)


def test_scores_without_a_blind_preferred_side_do_not_claim_baseline_comparison() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/local-v1.yaml"))
    no_preference = _report(preferred_side=None)

    summary = assess_corpus_acceptance(evaluation, [(no_preference, [])])

    assert summary.complete_human_coverage is True
    assert summary.complete_baseline_content_comparison is False
    assert summary.content_improves_over_baseline is False
    assert summary.accepted is False
    assert any("Missing blind content preference" in blocker for blocker in summary.blockers)
