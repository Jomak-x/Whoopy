from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from whoopy.cli import main
from whoopy.model_packs.legacy_cleanup import (
    LegacyCleanupError,
    LegacyCleanupManifest,
    LegacyRuntimeCleanup,
    load_legacy_cleanup_manifest,
)
from whoopy.model_packs.operations import HeavyweightModelSlot
from whoopy.pipeline.locks import RunLock


def _manifest(path: Path) -> Path:
    document = {
        "schema_version": 1,
        "roots": [
            {
                "root_id": "fish-legacy",
                "relative_path": "experimental/fish-runtime",
                "targets": [
                    {"category": "virtualenv", "path": ".venv/lib"},
                    {"category": "git", "path": ".git"},
                    {"category": "source", "path": "fish_speech"},
                    {
                        "category": "stale-download",
                        "glob": ("checkpoints/**/.cache/huggingface/download/*.incomplete"),
                    },
                ],
            }
        ],
    }
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _service(tmp_path: Path) -> tuple[LegacyRuntimeCleanup, Path, Path, Path]:
    models = tmp_path / "models"
    runs = tmp_path / "runs"
    root = models / "experimental" / "fish-runtime"
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "package.py").write_bytes(b"runtime")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / "fish_speech").mkdir()
    (root / "fish_speech" / "model.py").write_bytes(b"source")
    checkpoint = root / "checkpoints" / "voice"
    checkpoint.mkdir(parents=True)
    weight = checkpoint / "model.safetensors"
    weight.write_bytes(b"irreplaceable-checkpoint")
    stale = checkpoint / ".cache" / "huggingface" / "download" / "shard.incomplete"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial")
    manifest = _manifest(tmp_path / "cleanup.yaml")
    service = LegacyRuntimeCleanup.from_path(
        manifest,
        models_root=models,
        runs_root=runs,
    )
    return service, root, weight, stale


def test_inspect_is_manifest_driven_and_never_selects_checkpoint_weights(
    tmp_path: Path,
) -> None:
    service, root, weight, stale = _service(tmp_path)

    plan = service.inspect()

    selected = {candidate.source_relative_path for candidate in plan.candidates}
    assert selected == {
        "experimental/fish-runtime/.git",
        "experimental/fish-runtime/.venv/lib",
        "experimental/fish-runtime/fish_speech",
        (
            "experimental/fish-runtime/checkpoints/voice/.cache/"
            "huggingface/download/shard.incomplete"
        ),
    }
    assert weight.is_file()
    assert stale.is_file()
    assert plan.reclaimable_bytes == len(b"runtime") + len(b"ref: refs/heads/main\n") + len(
        b"source"
    ) + len(b"partial")
    assert plan.confirmation == f"CLEANUP {plan.plan_id}"
    assert plan.blocked_reason is None
    assert root.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not generally available")
def test_directory_hash_refuses_contained_symlinks(tmp_path: Path) -> None:
    service, root, _weight, _stale = _service(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must-not-be-read-or-moved")
    (root / ".venv" / "lib" / "outside-link").symlink_to(outside)

    with pytest.raises(LegacyCleanupError, match="containing symlink"):
        service.inspect()

    assert outside.read_bytes() == b"must-not-be-read-or-moved"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not generally available")
def test_selected_symlink_and_symlinked_ancestor_are_refused(tmp_path: Path) -> None:
    service, root, _weight, _stale = _service(tmp_path)
    real = tmp_path / "real-venv"
    real.mkdir()
    (root / ".venv" / "lib" / "package.py").unlink()
    (root / ".venv" / "lib").rmdir()
    (root / ".venv" / "lib").symlink_to(real, target_is_directory=True)

    with pytest.raises(LegacyCleanupError, match="symlink"):
        service.inspect()


def test_cleanup_is_recoverable_and_receipt_records_both_hashes(tmp_path: Path) -> None:
    service, _root, weight, stale = _service(tmp_path)
    plan = service.inspect()

    with pytest.raises(LegacyCleanupError, match="Explicit confirmation"):
        service.apply(confirmation="yes")

    receipt = service.apply(confirmation=plan.confirmation)

    assert receipt.state == "quarantined"
    assert all(item.quarantined == item.before for item in receipt.items)
    assert all(item.restored is None for item in receipt.items)
    assert weight.read_bytes() == b"irreplaceable-checkpoint"
    assert not stale.exists()
    assert len(service.receipts()) == 1
    receipt_file = service.receipts_root / f"{receipt.receipt_id}.json"
    assert receipt_file.is_file()

    with pytest.raises(LegacyCleanupError, match="Explicit confirmation"):
        service.restore(receipt.receipt_id, confirmation="restore")

    restored = service.restore(
        receipt.receipt_id,
        confirmation=service.restore_confirmation(receipt.receipt_id),
    )

    assert restored.state == "restored"
    assert restored.restored_at is not None
    assert all(item.restored == item.before for item in restored.items)
    assert stale.read_bytes() == b"partial"
    assert weight.read_bytes() == b"irreplaceable-checkpoint"
    # Restoring an already restored receipt is safe and idempotent.
    assert (
        service.restore(
            receipt.receipt_id,
            confirmation=service.restore_confirmation(receipt.receipt_id),
        )
        == restored
    )


def test_restore_resumes_after_rename_before_receipt_update(tmp_path: Path) -> None:
    service, _root, _weight, _stale = _service(tmp_path)
    receipt = service.apply(confirmation=service.inspect().confirmation)
    item = receipt.items[0]
    source = service.models_root / item.quarantine_relative_path
    destination = service.models_root / item.source_relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)

    completed = service.restore(
        receipt.receipt_id,
        confirmation=service.restore_confirmation(receipt.receipt_id),
    )

    assert completed.state == "restored"
    assert all(entry.restored == entry.before for entry in completed.items)
    assert destination.exists()
    assert not source.exists()


def test_stale_plan_confirmation_is_rejected(tmp_path: Path) -> None:
    service, root, _weight, _stale = _service(tmp_path)
    old_plan = service.inspect()
    (root / "fish_speech" / "model.py").write_bytes(b"changed")

    with pytest.raises(LegacyCleanupError, match="Explicit confirmation"):
        service.apply(confirmation=old_plan.confirmation)

    assert (root / "fish_speech" / "model.py").read_bytes() == b"changed"


def test_active_heavy_model_and_generation_worker_block_apply(tmp_path: Path) -> None:
    service, _root, _weight, _stale = _service(tmp_path)

    with HeavyweightModelSlot(service.models_root, "fish-speech-1.4"):
        plan = service.inspect()
        assert plan.blocked_reason == (
            "Cleanup is blocked while a heavyweight model runtime is active."
        )
        with pytest.raises(LegacyCleanupError, match="heavyweight model runtime"):
            service.apply(confirmation=plan.confirmation)

    run_directory = service.runs_root / "run-1"  # type: ignore[operator]
    run_directory.mkdir(parents=True)
    with RunLock(run_directory):
        plan = service.inspect()
        assert "generation run run-1" in (plan.blocked_reason or "")
        with pytest.raises(LegacyCleanupError, match="generation run run-1"):
            service.apply(confirmation=plan.confirmation)


def test_tampered_quarantine_is_never_restored(tmp_path: Path) -> None:
    service, _root, _weight, _stale = _service(tmp_path)
    plan = service.inspect()
    receipt = service.apply(confirmation=plan.confirmation)
    item = next(entry for entry in receipt.items if entry.category == "source")
    payload = service.models_root / item.quarantine_relative_path / "model.py"
    payload.write_bytes(b"tampered")

    with pytest.raises(LegacyCleanupError, match="failed receipt verification"):
        service.restore(
            receipt.receipt_id,
            confirmation=service.restore_confirmation(receipt.receipt_id),
        )

    assert not (service.models_root / item.source_relative_path).exists()


def test_purge_requires_exact_confirmation_and_reclaims_verified_payload(
    tmp_path: Path,
) -> None:
    service, _root, weight, _stale = _service(tmp_path)
    plan = service.inspect()
    receipt = service.apply(confirmation=plan.confirmation)

    with pytest.raises(LegacyCleanupError, match="irreversible purge confirmation"):
        service.purge(receipt.receipt_id, confirmation="purge")

    purged = service.purge(
        receipt.receipt_id,
        confirmation=service.purge_confirmation(receipt.receipt_id),
    )

    assert purged.state == "purged"
    assert purged.purged_at is not None
    assert purged.purged_bytes == plan.reclaimable_bytes
    assert all(item.purged for item in purged.items)
    assert weight.read_bytes() == b"irreplaceable-checkpoint"
    assert (
        service.purge(
            receipt.receipt_id,
            confirmation=service.purge_confirmation(receipt.receipt_id),
        )
        == purged
    )
    with pytest.raises(LegacyCleanupError, match="irreversibly purged"):
        service.restore(
            receipt.receipt_id,
            confirmation=service.restore_confirmation(receipt.receipt_id),
        )


def test_purge_refuses_tampered_or_non_quarantined_receipts(tmp_path: Path) -> None:
    service, _root, _weight, _stale = _service(tmp_path)
    plan = service.inspect()
    receipt = service.apply(confirmation=plan.confirmation)
    item = next(entry for entry in receipt.items if entry.category == "source")
    payload = service.models_root / item.quarantine_relative_path / "model.py"
    payload.write_bytes(b"tampered")

    with pytest.raises(LegacyCleanupError, match="failed receipt verification"):
        service.purge(
            receipt.receipt_id,
            confirmation=service.purge_confirmation(receipt.receipt_id),
        )

    assert payload.read_bytes() == b"tampered"

    # A restored receipt no longer owns quarantine data and cannot be purged.
    payload.write_bytes(b"source")
    restored = service.restore(
        receipt.receipt_id,
        confirmation=service.restore_confirmation(receipt.receipt_id),
    )
    with pytest.raises(LegacyCleanupError, match="fully quarantined"):
        service.purge(
            restored.receipt_id,
            confirmation=service.purge_confirmation(restored.receipt_id),
        )


def test_purge_resumes_after_delete_before_receipt_update(tmp_path: Path) -> None:
    service, _root, _weight, _stale = _service(tmp_path)
    plan = service.inspect()
    receipt = service.apply(confirmation=plan.confirmation)
    first = receipt.items[0]
    payload = service.models_root / first.quarantine_relative_path
    if payload.is_dir():
        import shutil

        shutil.rmtree(payload)
    else:
        payload.unlink()
    # Simulate the durable state that is written before purge starts and a
    # crash after the first delete but before that item's receipt update.
    purging = receipt.model_copy(update={"state": "purging"})
    service._write_receipt(purging)

    completed = service.purge(
        receipt.receipt_id,
        confirmation=service.purge_confirmation(receipt.receipt_id),
    )

    assert completed.state == "purged"
    assert all(item.purged for item in completed.items)


@pytest.mark.parametrize(
    "target",
    [
        {"category": "source", "path": "../checkpoints"},
        {"category": "source", "path": "checkpoints/voice"},
        {"category": "stale-download", "glob": "checkpoints/**/*.safetensors"},
        {"category": "source", "glob": "**/*"},
    ],
)
def test_manifest_rejects_path_escape_and_checkpoint_weight_selectors(
    target: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        LegacyCleanupManifest.model_validate(
            {
                "schema_version": 1,
                "roots": [
                    {
                        "root_id": "unsafe",
                        "relative_path": "experimental/runtime",
                        "targets": [target],
                    }
                ],
            }
        )


def test_receipt_path_traversal_is_refused(tmp_path: Path) -> None:
    service, _root, _weight, _stale = _service(tmp_path)

    with pytest.raises(LegacyCleanupError, match="Invalid cleanup receipt ID"):
        service.restore("../../escape", confirmation="RESTORE ../../escape")


def test_tampered_receipt_cannot_redirect_restore_outside_allowlist(tmp_path: Path) -> None:
    service, _root, _weight, _stale = _service(tmp_path)
    receipt = service.apply(confirmation=service.inspect().confirmation)
    receipt_path = service.receipts_root / f"{receipt.receipt_id}.json"
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["items"][0]["source_relative_path"] = "managed/important-model.bin"
    receipt_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LegacyCleanupError, match=r"mismatched quarantine metadata|allowlist"):
        service.restore(
            receipt.receipt_id,
            confirmation=service.restore_confirmation(receipt.receipt_id),
        )


def test_cleanup_cli_inspect_is_json_and_does_not_move_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service, _root, weight, stale = _service(tmp_path)
    manifest_path = tmp_path / "cleanup.yaml"

    exit_code = main(
        [
            "models",
            "cleanup",
            "inspect",
            "--cleanup-manifest",
            str(manifest_path),
            "--models-dir",
            str(service.models_root),
            "--runs-dir",
            str(service.runs_root),
            "--json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["reclaimable_bytes"] > 0
    assert output["confirmation"].startswith("CLEANUP ")
    assert weight.is_file()
    assert stale.is_file()


def test_cleanup_cli_apply_and_purge_use_separate_exact_confirmations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service, _root, weight, _stale = _service(tmp_path)
    common = [
        "--cleanup-manifest",
        str(tmp_path / "cleanup.yaml"),
        "--models-dir",
        str(service.models_root),
        "--runs-dir",
        str(service.runs_root),
        "--json",
    ]
    plan = service.inspect()

    assert main(["models", "cleanup", "apply", "--confirm", plan.confirmation, *common]) == 0
    applied = json.loads(capsys.readouterr().out)
    receipt_id = applied["receipt_id"]
    assert applied["state"] == "quarantined"

    assert (
        main(
            [
                "models",
                "cleanup",
                "purge",
                receipt_id,
                "--confirm",
                f"PURGE {receipt_id}",
                *common,
            ]
        )
        == 0
    )
    purged = json.loads(capsys.readouterr().out)
    assert purged["state"] == "purged"
    assert purged["purged_bytes"] == plan.reclaimable_bytes
    assert weight.is_file()


def test_checked_in_manifest_is_valid_and_never_exactly_selects_checkpoints() -> None:
    manifest = load_legacy_cleanup_manifest(Path("config/legacy_cleanup.yaml"))

    assert {root.root_id for root in manifest.roots} == {
        "fish-speech-1.4-legacy",
        "moss-tts-legacy",
    }
    assert all(
        target.path is None or not target.path.startswith("checkpoints/")
        for root in manifest.roots
        for target in root.targets
    )
