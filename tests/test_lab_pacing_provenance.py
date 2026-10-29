from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from whoopy.adapters.tts.sherpa_onnx import SherpaOnnxKokoroAdapter, SherpaOnnxSettings
from whoopy.evaluation.lab import EvaluationExecutionError, create_promotion_decision
from whoopy.evaluation.models import (
    EvaluationCondition,
    ExperimentCandidate,
    ExperimentDefinition,
    ExperimentReport,
    ProductionTrialResult,
    VoiceConfiguration,
)
from whoopy.evaluation.pacing_profiles import (
    PacingProfile,
    PacingProfileError,
    PacingProfileRegistry,
    apply_pacing_profile,
)
from whoopy.timeline import SilenceSegment, build_script_timeline
from whoopy.webui.evaluation import LocalProductionExperimentExecutor


def _timeline() -> dict[str, Any]:
    return {
        "segments": [
            {
                "id": "speech-0001",
                "type": "SPEECH",
                "text": "Settle here.",
                "delivery": "calm",
                "intensity": "subtle",
            },
            {"id": "silence-0001", "type": "SILENCE", "duration_ms": 2_000},
            {
                "id": "breath-0001",
                "type": "BREATH",
                "duration_ms": 4_000,
                "phase": "inhale",
                "cycle": 1,
                "protocol_id": "gentle-even@1",
            },
        ]
    }


def test_reviewed_pacing_registry_is_versioned_and_content_addressed() -> None:
    registry = PacingProfileRegistry.load(Path("config/pacing_profiles.yaml"))
    baseline = registry.get("meditative-v2")
    progressive = registry.get("progressive-quiet-v2")

    assert baseline.versioned_id == "meditative-v2@2"
    assert baseline.allowed_timeline_mutations == ()
    assert progressive.allowed_timeline_mutations == ("SILENCE.duration_ms",)
    assert baseline.digest != progressive.digest
    assert baseline.digest == registry.get("meditative-v2").digest
    bundled = PacingProfileRegistry.load_bundled()
    assert bundled.document == registry.document


def test_missing_custom_registry_uses_bundled_but_invalid_override_is_fatal(
    tmp_path: Path,
) -> None:
    application = SimpleNamespace(
        runs_directory=tmp_path / "runs",
        config_directory=tmp_path / "missing-config",
    )
    executor = LocalProductionExperimentExecutor(cast(Any, application))
    assert executor.pacing_profiles.get("meditative-v2").version == 2

    config = tmp_path / "invalid-config"
    config.mkdir()
    (config / "pacing_profiles.yaml").write_text("profiles: invalid\n", encoding="utf-8")
    application.config_directory = config
    with pytest.raises(PacingProfileError, match="Could not load pacing profiles"):
        LocalProductionExperimentExecutor(cast(Any, application))


def test_pacing_profile_speed_reaches_speech_cache_identity(tmp_path: Path) -> None:
    model_root = tmp_path / "kokoro"
    (model_root / "espeak-ng-data").mkdir(parents=True)
    for filename in ("model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt"):
        (model_root / filename).write_bytes(b"fixture")
    registry = PacingProfileRegistry.load(Path("config/pacing_profiles.yaml"))
    meditative = registry.get("meditative-v2")
    focus = registry.get("focus")
    first = SherpaOnnxKokoroAdapter(
        model_directory=tmp_path,
        model_version="fixture",
        runtime_version="fixture",
        license_id="Apache-2.0",
        settings=SherpaOnnxSettings(speed=meditative.speech_speed),
    )
    second = SherpaOnnxKokoroAdapter(
        model_directory=tmp_path,
        model_version="fixture",
        runtime_version="fixture",
        license_id="Apache-2.0",
        settings=SherpaOnnxSettings(speed=focus.speech_speed),
    )

    assert first.cache_identity != second.cache_identity


def test_progressive_studio_pacing_preserves_topology_breaths_and_total_quiet() -> None:
    timeline = build_script_timeline(
        run_id=uuid4(),
        script=(
            "Arrive.\n\n[pause: 4s]\n\nNotice.\n\n[pause: 8s]\n\n"
            "Continue.\n\n[pause: 12s]\n\nReturn."
        ),
        created_at=datetime.now(UTC),
    )
    profile = PacingProfileRegistry.load(Path("config/pacing_profiles.yaml")).get(
        "progressive-quiet-v2"
    )

    paced = apply_pacing_profile(timeline, profile)
    before = [
        segment.duration_ms for segment in timeline.segments if isinstance(segment, SilenceSegment)
    ]
    after = [
        segment.duration_ms for segment in paced.segments if isinstance(segment, SilenceSegment)
    ]

    assert [segment.type for segment in paced.segments] == [
        segment.type for segment in timeline.segments
    ]
    assert sum(after) == sum(before)
    assert after != before
    assert after[-1] > before[-1]
    assert (
        apply_pacing_profile(timeline, PacingProfileRegistry.load_bundled().get("meditative-v2"))
        == timeline
    )


def test_topology_hash_covers_interleaving_but_not_allowlisted_quiet_duration() -> None:
    canonical = _timeline()
    changed_duration = _timeline()
    changed_duration["segments"][1]["duration_ms"] = 8_000
    reordered = _timeline()
    reordered["segments"][1], reordered["segments"][2] = (
        reordered["segments"][2],
        reordered["segments"][1],
    )

    assert LocalProductionExperimentExecutor._timeline_topology_hash(
        canonical
    ) == LocalProductionExperimentExecutor._timeline_topology_hash(changed_duration)
    assert LocalProductionExperimentExecutor._timeline_topology_hash(
        canonical
    ) != LocalProductionExperimentExecutor._timeline_topology_hash(reordered)


def test_pacing_mutation_allowlist_rejects_breath_or_topology_drift() -> None:
    canonical = _timeline()
    allowed = _timeline()
    allowed["segments"][1]["duration_ms"] = 8_000
    LocalProductionExperimentExecutor._attest_timeline_mutations(
        canonical,
        allowed,
        allowed_mutations={"SILENCE.duration_ms"},
    )

    changed_breath = _timeline()
    changed_breath["segments"][2]["duration_ms"] = 5_000
    with pytest.raises(ValueError, match=r"BREATH\.duration_ms"):
        LocalProductionExperimentExecutor._attest_timeline_mutations(
            canonical,
            changed_breath,
            allowed_mutations={"SILENCE.duration_ms"},
        )


def test_failed_candidate_is_persisted_as_explicit_incomplete_comparison() -> None:
    condition = EvaluationCondition(
        llm_model_id="qwen3-4b",
        writing_method="blueprint_first_sparse",
        voice=VoiceConfiguration(
            configuration_id="kokoro-heart",
            model_id="kokoro",
            voice_id="af_heart",
        ),
        pacing_profile_id="meditative-v2",
    )
    definition = ExperimentDefinition(
        experiment_id="explicit-incomplete",
        version=1,
        experiment_type="pacing",
        evaluation_id="fixture",
        evaluation_version=1,
        seed=1,
        baseline_candidate_id="baseline",
        candidates=[
            ExperimentCandidate(candidate_id="baseline", label="Baseline", condition=condition),
            ExperimentCandidate(
                candidate_id="candidate",
                label="Candidate",
                condition=condition.model_copy(
                    update={"pacing_profile_id": "progressive-quiet-v2"}
                ),
            ),
        ],
    )
    now = datetime.now(UTC)
    failed = ProductionTrialResult(
        trial_id=uuid4(),
        source_run_id=uuid4(),
        candidate_id="baseline",
        case_id="case-1",
        success=False,
        actual_condition=condition,
        started_at=now,
        completed_at=now,
        failure="runtime failed",
    )
    report = ExperimentReport(
        definition=definition,
        created_at=now,
        platform="fixture",
        trials=[failed],
        comparison_status="incomplete",
        incomplete_candidates_by_case={"case-1": ["baseline", "candidate"]},
    )

    assert report.comparison_status == "incomplete"
    assert report.incomplete_candidates_by_case == {"case-1": ["baseline", "candidate"]}
    dumped = report.model_dump(mode="json")
    assert dumped["comparison_status"] == "incomplete"
    assert dumped["incomplete_candidates_by_case"]["case-1"]
    with pytest.raises(EvaluationExecutionError, match="complete comparison"):
        create_promotion_decision(
            report,
            outcome="promote",
            candidate_id="candidate",
            previous_value="baseline",
            selected_value="candidate",
            rationale="Should not be accepted from partial evidence.",
            report_sha256="a" * 64,
        )


def test_canonical_provenance_pins_active_revision_artifact_and_digest(tmp_path: Path) -> None:
    run_id = uuid4()
    root = tmp_path / str(run_id)
    (root / "revisions").mkdir(parents=True)
    timeline = _timeline()
    timeline_path = root / "timeline.json"
    timeline_path.write_text(json.dumps(timeline, sort_keys=True), encoding="utf-8")
    (root / "revisions" / "current.json").write_text(
        '{"revision_id":"r0003-abc123def4"}', encoding="utf-8"
    )
    application = SimpleNamespace(runs_directory=tmp_path)
    executor = LocalProductionExperimentExecutor(cast(Any, application))

    provenance = executor._canonical_source_provenance(run_id, timeline)

    assert provenance.source_run_id == run_id
    assert provenance.source_revision_id == "r0003-abc123def4"
    assert provenance.artifact_path == str(timeline_path)
    assert provenance.artifact_sha256 == hashlib.sha256(timeline_path.read_bytes()).hexdigest()
    assert provenance.topology_sha256 == executor._timeline_topology_hash(timeline)

    timeline_path.write_text('{"segments":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="artifact changed"):
        executor._verify_canonical_source(provenance)


def test_pacing_profile_validation_binds_strategy_to_mutation_policy() -> None:
    with pytest.raises(ValueError, match="mutation allowlist"):
        PacingProfile(
            profile_id="unsafe-profile",
            version=1,
            label="Unsafe",
            speech_speed=0.6,
            silence_strategy="preserve",
            allowed_timeline_mutations=("SILENCE.duration_ms",),
        )
