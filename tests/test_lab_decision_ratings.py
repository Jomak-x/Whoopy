from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_evaluation_acceptance import _report, _write_release_report
from whoopy.evaluation import ExperimentReport, PromotionDecision
from whoopy.evaluation.lab import create_promotion_decision
from whoopy.release.status import assess_lab


def _complete_lab(lab_root: Path) -> Path:
    decisions = lab_root / "decisions"
    decisions.mkdir(parents=True)
    for version, kind in enumerate(("llm", "writing_method", "voice", "pacing"), start=1):
        report = _report()
        baseline, candidate = report.definition.candidates
        condition = baseline.condition
        changes: dict[str, dict[str, object]] = {
            "llm": {"llm_model_id": "fixture-standard"},
            "writing_method": {"writing_method": candidate.condition.writing_method},
            "voice": {"voice": condition.voice.model_copy(update={"voice_id": "am_michael"})},
            "pacing": {"pacing_profile_id": "fixture-pacing"},
        }
        candidates = [
            baseline,
            candidate.model_copy(update={"condition": condition.model_copy(update=changes[kind])}),
        ]
        conditions = {item.candidate_id: item.condition for item in candidates}
        report = report.model_copy(
            update={
                "definition": report.definition.model_copy(
                    update={
                        "experiment_id": f"fixture-{kind}",
                        "experiment_type": kind,
                        "candidates": candidates,
                    }
                ),
                "trials": [
                    trial.model_copy(update={"actual_condition": conditions[trial.candidate_id]})
                    for trial in report.trials
                ],
            }
        )
        _write_release_report(lab_root, report, version=version)
        path = lab_root / "experiments" / f"fixture-{kind}" / f"v{version}" / "report.json"
        stored = ExperimentReport.model_validate_json(path.read_text()).model_copy(
            update={"ratings": report.ratings}
        )
        path.write_text(stored.model_dump_json())
        decision = PromotionDecision(
            decision_id=uuid4(),
            created_at=datetime.now(UTC),
            experiment_id=stored.definition.experiment_id,
            experiment_version=version,
            outcome="promote",
            studio_target=stored.definition.experiment_type,
            selection_role="portable_default" if kind == "llm" else None,
            candidate_id="candidate",
            previous_value="fixture-baseline",
            selected_value="fixture-candidate",
            rationale="Synthetic gate fixture with bound ratings.",
            report_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            rating_ids=[rating.rating_id for rating in stored.ratings],
            rating_sha256_by_id={
                str(rating.rating_id): hashlib.sha256(
                    json.dumps(
                        rating.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                for rating in stored.ratings
            },
        )
        (decisions / f"{decision.decision_id}.json").write_text(decision.model_dump_json())
        if kind == "llm":
            fallback = decision.model_copy(
                update={
                    "decision_id": uuid4(),
                    "selection_role": "low_resource_fallback",
                    "candidate_id": "baseline",
                    "selected_value": "fixture-fallback",
                }
            )
            (decisions / f"{fallback.decision_id}.json").write_text(fallback.model_dump_json())
    report = _report()
    _write_release_report(lab_root, report, version=5)
    return lab_root / "experiments" / report.definition.experiment_id / "v5" / "report.json"


@pytest.mark.parametrize("rating_state", ["none", "valid", "missing", "changed", "promotion"])
def test_release_gate_accepts_unrated_rejection_but_validates_supplied_ratings(
    tmp_path: Path, rating_state: str
) -> None:
    lab_root = tmp_path / "runs" / ".whoopy-lab"
    report_path = _complete_lab(lab_root)
    comparisons, decisions = assess_lab(lab_root)
    assert comparisons.outcome == "passed"
    assert decisions.outcome == "passed"
    report = ExperimentReport.model_validate_json(report_path.read_text())
    if rating_state in {"valid", "missing", "changed"}:
        report = report.model_copy(update={"ratings": _report().ratings})
        report_path.write_text(report.model_dump_json())
    decision = create_promotion_decision(
        report,
        outcome="reject",
        candidate_id="candidate",
        previous_value="fixture-baseline",
        selected_value="fixture-baseline",
        rationale="Synthetic rejection; no human score is required to decline a candidate.",
        report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
    )
    if rating_state == "missing":
        report = report.model_copy(update={"ratings": []})
        report_path.write_text(report.model_dump_json())
        decision = decision.model_copy(
            update={"report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest()}
        )
    elif rating_state == "changed":
        decision = decision.model_copy(
            update={
                "rating_sha256_by_id": {str(rating): "0" * 64 for rating in decision.rating_ids}
            }
        )
    elif rating_state == "promotion":
        decision = decision.model_copy(update={"outcome": "promote"})
    decision_path = lab_root / "decisions" / f"{decision.decision_id}.json"
    decision_path.write_text(decision.model_dump_json())
    original = decision_path.read_bytes()

    _comparisons, result = assess_lab(lab_root)

    assert result.outcome == ("passed" if rating_state in {"none", "valid"} else "failed")
    assert decision_path.read_bytes() == original
    if rating_state == "missing":
        assert any("supporting rating IDs" in blocker for blocker in result.blockers)
    elif rating_state == "changed":
        assert any(
            "supporting rating" in blocker and "stale" in blocker for blocker in result.blockers
        )
    elif rating_state == "promotion":
        assert any(
            "promotions require durable supporting ratings" in blocker
            for blocker in result.blockers
        )
