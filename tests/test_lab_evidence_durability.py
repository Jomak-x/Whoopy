from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_evaluation_acceptance import _report
from whoopy.evaluation.models import PromotionDecision
from whoopy.evaluation.storage import EvaluationRepository, EvaluationStorageError
from whoopy.pipeline.runs import RunStore


@pytest.mark.parametrize("kind", ["report", "rating", "decision"])
@pytest.mark.parametrize("sync_fails", [False, True])
def test_immutable_evidence_syncs_published_entry_before_acknowledging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, sync_fails: bool
) -> None:
    repository = EvaluationRepository(tmp_path)
    report = _report()
    save: Callable[[], Path]
    if kind == "report":
        expected = report.model_dump_json(indent=2) + "\n"
        save = partial(repository.save_report, report)
    elif kind == "rating":
        rating = report.ratings[0]
        expected = rating.model_dump_json(indent=2) + "\n"
        save = partial(
            repository.save_rating,
            experiment_id=report.definition.experiment_id,
            experiment_version=report.definition.version,
            rating=rating,
        )
    else:
        decision = PromotionDecision(
            decision_id=uuid4(),
            created_at=datetime.now(UTC),
            experiment_id=report.definition.experiment_id,
            experiment_version=report.definition.version,
            outcome="reject",
            studio_target="writing_method",
            candidate_id="candidate",
            previous_value="baseline",
            selected_value="baseline",
            rationale="Synthetic durability fixture.",
            report_sha256="a" * 64,
        )
        expected = decision.model_dump_json(indent=2) + "\n"
        save = partial(repository.save_decision, decision)
    synchronized: list[Path] = []
    sync_directory = RunStore._fsync_directory

    def sync(directory: Path) -> None:
        entries = list(directory.iterdir())
        assert len(entries) == 1
        assert entries[0].suffix == ".json"
        assert entries[0].read_text() == expected
        synchronized.append(entries[0])
        if sync_fails:
            raise OSError("evidence directory sync failed")
        sync_directory(directory)

    monkeypatch.setattr(RunStore, "_fsync_directory", staticmethod(sync))
    if sync_fails:
        with pytest.raises(EvaluationStorageError, match="evidence directory sync failed"):
            save()
    else:
        assert save() == synchronized[0]
    assert len(synchronized) == 1
    assert synchronized[0].read_text() == expected
    with pytest.raises(EvaluationStorageError, match="Refusing to replace immutable"):
        save()
    assert len(synchronized) == 1
    assert synchronized[0].read_text() == expected
