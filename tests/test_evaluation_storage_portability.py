from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from whoopy.evaluation import storage
from whoopy.evaluation.models import HumanRating
from whoopy.evaluation.storage import EvaluationRepository, EvaluationStorageError


@pytest.fixture
def writable_flushes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    flushed: list[int] = []

    def flush(descriptor: int) -> None:
        # Windows requires write access for fsync; a zero-byte write enforces
        # the same requirement on POSIX without changing the stored evidence.
        os.write(descriptor, b"")
        os.fsync(descriptor)
        flushed.append(descriptor)

    monkeypatch.setattr(storage, "os", SimpleNamespace(**(vars(os) | {"fsync": flush})))
    return flushed


def test_immutable_evidence_flushes_writable_handle_before_publication(
    tmp_path: Path, writable_flushes: list[int]
) -> None:
    repository = EvaluationRepository(tmp_path)
    rating = HumanRating(
        rating_id=uuid4(),
        assignment_id=uuid4(),
        dimension="voice",
        score=4,
        preferred_side="right",
        notes="Comfortable delivery.",
        created_at=datetime.now(UTC),
    )

    path = repository.save_rating(experiment_id="portable", experiment_version=1, rating=rating)

    assert repository.ratings(experiment_id="portable", experiment_version=1) == [rating]
    assert len(writable_flushes) == 1
    original = path.read_bytes()
    with pytest.raises(EvaluationStorageError, match="Refusing to replace immutable"):
        repository.save_rating(
            experiment_id="portable",
            experiment_version=1,
            rating=rating.model_copy(update={"score": 1}),
        )
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


def test_mutable_control_flushes_writable_handle_on_creation_and_replacement(
    tmp_path: Path, writable_flushes: list[int]
) -> None:
    repository = EvaluationRepository(tmp_path)

    path = repository.save_execution_control(
        experiment_id="portable", version=1, payload={"status": "running"}
    )
    assert repository.load_execution_control(experiment_id="portable", version=1) == {
        "status": "running"
    }
    repository.save_execution_control(
        experiment_id="portable", version=1, payload={"status": "paused"}
    )

    assert repository.load_execution_control(experiment_id="portable", version=1) == {
        "status": "paused"
    }
    assert len(writable_flushes) == 2
    assert list(path.parent.iterdir()) == [path]
