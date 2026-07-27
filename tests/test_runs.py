from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.control import LocalControlPlane
from whoopy.pipeline.runs import InvalidRunIdError, RunStatus, RunStore, RunStoreError

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
CREATED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_control_plane_saves_a_queued_run_without_processing_it(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    control = LocalControlPlane(store)

    record = control.submit_prompt("  A short grounding meditation.  ")

    assert record.status is RunStatus.QUEUED
    assert record.prompt == "A short grounding meditation."
    assert store.load(record.run_id) == record
    assert store.record_path(record.run_id).is_file()
    assert not store.timeline_path(record.run_id).exists()

    document = json.loads(store.record_path(record.run_id).read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["run_id"] == str(record.run_id)
    assert document["status"] == "queued"
    assert control.get_run(str(record.run_id)) == record


def test_empty_prompt_is_rejected_before_a_directory_is_created(tmp_path: Path) -> None:
    store = RunStore(tmp_path)

    with pytest.raises(RunStoreError, match="Invalid run request"):
        store.create("   ", run_id=RUN_ID, created_at=CREATED_AT)

    assert not store.run_directory(RUN_ID).exists()


def test_run_id_must_be_a_uuid_before_it_becomes_a_path(tmp_path: Path) -> None:
    store = RunStore(tmp_path)

    with pytest.raises(InvalidRunIdError, match="Invalid run ID"):
        store.load("../../outside")
