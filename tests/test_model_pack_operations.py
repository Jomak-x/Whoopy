from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from whoopy.model_packs.manager import ManagedModelPacks, SmokeResult
from whoopy.model_packs.operations import (
    AcceleratorUsage,
    HeavyweightModelSlot,
    HeavyweightModelSlotUnavailable,
    ManagedPackTrash,
    ManagedTrashError,
    ModelPerformanceRecorder,
    PackInstallUnavailable,
    PackProgressStore,
    PerformanceRecordStore,
    ProgressConflictError,
    TransferState,
)


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source = str(Path("src").resolve())
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not existing else os.pathsep.join((source, existing))
    return environment


def _slot_child(root: Path, *, hold: bool = False) -> subprocess.Popen[str]:
    source = """
import sys
import time
from pathlib import Path
from whoopy.model_packs.operations import HeavyweightModelSlot, HeavyweightModelSlotUnavailable

slot = HeavyweightModelSlot(Path(sys.argv[1]), "moss-local-5b")
try:
    slot.acquire(timeout_seconds=0.25)
except HeavyweightModelSlotUnavailable:
    print("blocked", flush=True)
    raise SystemExit(17)
print("acquired", flush=True)
if sys.argv[2] == "hold":
    time.sleep(30)
slot.release()
"""
    return subprocess.Popen(
        [sys.executable, "-c", source, str(root), "hold" if hold else "release"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )


def _install_lock_child(root: Path, *, hold: bool = False) -> subprocess.Popen[str]:
    source = """
import sys
import time
from pathlib import Path
from whoopy.model_packs.operations import PackInstallLock

lock = PackInstallLock(Path(sys.argv[1]), "test-pack")
lock.acquire()
print("acquired", flush=True)
if sys.argv[2] == "hold":
    time.sleep(30)
lock.release()
"""
    return subprocess.Popen(
        [sys.executable, "-c", source, str(root), "hold" if hold else "release"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )


def _interrupted_install_child(root: Path, *, artifact_size: int) -> subprocess.Popen[str]:
    source = """
import sys
import time
from pathlib import Path
from whoopy.model_packs.operations import PackInstallLock, PackProgressStore

root = Path(sys.argv[1])
lock = PackInstallLock(root, "test-pack")
lock.acquire()
store = PackProgressStore(
    root / "managed" / "model-packs" / "test-pack" / "records",
    safety_root=root,
)
store.start(
    pack_id="test-pack",
    revision="revision-1",
    artifacts={"model.bin": int(sys.argv[2])},
)
print("started", flush=True)
time.sleep(30)
"""
    return subprocess.Popen(
        [sys.executable, "-c", source, str(root), str(artifact_size)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_environment(),
    )


def _managed_service(
    tmp_path: Path,
    payload: bytes = b"tiny-model",
    *,
    revision: str = "revision-1",
    existing_directory: str | None = None,
) -> tuple[ManagedModelPacks, Path]:
    models = tmp_path / "models"
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / "model.bin").write_bytes(payload)
    runtime = models / "managed" / "model-packs" / "test-pack" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "python").write_text("runtime", encoding="utf-8")
    registry_path = tmp_path / "model_packs.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "packs": [
                    {
                        "pack_id": "test-pack",
                        "display_name": "Test Pack",
                        "revision": revision,
                        "source_repository": "https://huggingface.co/example/test-pack",
                        "license_id": "Apache-2.0",
                        "license_url": "https://example.test/license",
                        "commercial_use_allowed": True,
                        "license_notice": "Test-only pack.",
                        "managed_directory": "managed/model-packs/test-pack/files",
                        "existing_directories": (
                            [existing_directory] if existing_directory is not None else []
                        ),
                        "supported_platforms": ["all"],
                        "files": [
                            {
                                "path": "model.bin",
                                "size_bytes": len(payload),
                                "digest": {
                                    "algorithm": "sha256",
                                    "value": __import__("hashlib").sha256(payload).hexdigest(),
                                },
                                "role": "model",
                            }
                        ],
                        "runtime": {
                            "runtime_id": "test-runtime",
                            "revision": "runtime-1",
                            "candidate_directories": ["managed/model-packs/test-pack/runtime"],
                            "required_markers": ["python"],
                        },
                        "hardware": {
                            "min_total_ram_gb": 0.01,
                            "min_available_ram_gb": 0.01,
                            "min_free_disk_gb": 0,
                            "accelerator_any_of": ["cpu"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = ManagedModelPacks.from_paths(registry_path, models)
    # Facade unit tests opt out explicitly; production from_paths installs the
    # real five-pack offline runner and its behavior is covered separately.
    service.smoke_runner = None
    return service, offline


def test_heavyweight_slot_rejects_real_cross_process_contention(tmp_path: Path) -> None:
    with HeavyweightModelSlot(tmp_path, "fish-speech-1.4"):
        child = _slot_child(tmp_path)
        stdout, stderr = child.communicate(timeout=5)

    assert child.returncode == 17, stderr
    assert stdout.strip() == "blocked"

    released_child = _slot_child(tmp_path)
    stdout, stderr = released_child.communicate(timeout=5)
    assert released_child.returncode == 0, stderr
    assert stdout.strip() == "acquired"


def test_heavyweight_slot_is_recovered_after_owner_process_is_killed(tmp_path: Path) -> None:
    child = _slot_child(tmp_path, hold=True)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "acquired"

    child.kill()
    child.wait(timeout=5)

    # The stale owner JSON is diagnostic only; the kernel lock is authoritative.
    slot = HeavyweightModelSlot(tmp_path, "fish-speech-1.4")
    owner = slot.acquire(timeout_seconds=1)
    try:
        assert owner.pack_id == "fish-speech-1.4"
        assert owner.pid == os.getpid()
    finally:
        slot.release()


def test_whole_install_lock_blocks_live_writer_and_recovers_after_kill(tmp_path: Path) -> None:
    service, offline = _managed_service(tmp_path)
    child = _install_lock_child(service.models_root, hold=True)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "acquired"

    try:
        with pytest.raises(PackInstallUnavailable, match="already installing"):
            service.install("test-pack", offline_directory=offline, allow_network=False)
    finally:
        child.kill()
        child.wait(timeout=5)

    # The OS releases the lock after SIGKILL; the next owner completes rather
    # than treating a stale lock file as a live transfer.
    report = service.install("test-pack", offline_directory=offline, allow_network=False)
    assert report.progress is not None
    assert report.progress.state is TransferState.COMPLETE


def test_install_resumes_durable_operation_after_writer_process_is_killed(tmp_path: Path) -> None:
    payload = b"durable-model"
    service, offline = _managed_service(tmp_path, payload=payload)
    child = _interrupted_install_child(service.models_root, artifact_size=len(payload))
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "started"
    child.kill()
    child.wait(timeout=5)

    report = service.install("test-pack", offline_directory=offline, allow_network=False)

    assert report.progress is not None
    assert report.progress.state is TransferState.COMPLETE
    assert report.progress.bytes_downloaded == len(payload)
    assert (
        service.models_root / "managed" / "model-packs" / "test-pack" / "files" / "model.bin"
    ).read_bytes() == payload


def test_slot_status_removes_stale_owner_when_kernel_lock_is_free(tmp_path: Path) -> None:
    slot = HeavyweightModelSlot(tmp_path, "probe")
    slot.owner_path.parent.mkdir(parents=True)
    slot.owner_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "token": "stale-token",
                "pack_id": "moss-8b",
                "pid": 999_999,
                "acquired_at": "2026-08-04T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    status = slot.status()

    assert not status.in_use
    assert status.owner is None
    assert not slot.owner_path.exists()


def test_unload_ignores_stale_owner_diagnostics(tmp_path: Path) -> None:
    service, _offline = _managed_service(tmp_path)
    slot = HeavyweightModelSlot(service.models_root, "probe")
    slot.owner_path.parent.mkdir(parents=True)
    slot.owner_path.write_text(
        '{"schema_version":1,"token":"stale","pack_id":"moss-8b",'
        '"pid":999999,"acquired_at":"2026-08-04T00:00:00Z"}',
        encoding="utf-8",
    )

    report = service.unload()

    assert report.pack_id is None
    assert "No heavyweight" in report.message
    assert not slot.owner_path.exists()


def test_heavyweight_slot_releases_after_context_exception(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError, match="model failed"),
        HeavyweightModelSlot(tmp_path, "moss-8b"),
    ):
        raise RuntimeError("model failed")

    with HeavyweightModelSlot(tmp_path, "fish-speech-1.4"):
        pass


def test_progress_is_byte_based_durable_and_monotonic(tmp_path: Path) -> None:
    store = PackProgressStore(tmp_path / "moss-8b" / "records")
    started = store.start(
        pack_id="moss-8b",
        revision="revision-1",
        artifacts={"index": 10, "shard-1": 100},
        operation_id="operation-1",
    )

    first = store.update_artifact("operation-1", "index", 10)
    resumed_store = PackProgressStore(tmp_path / "moss-8b" / "records")
    resumed = resumed_store.update_artifact("operation-1", "shard-1", 40)

    assert started.bytes_total == 110
    assert first.bytes_downloaded == 10
    assert resumed.bytes_downloaded == 50
    assert resumed_store.load() == resumed
    assert not list(store.records_directory.glob("*.tmp"))

    with pytest.raises(ProgressConflictError, match="move backwards"):
        resumed_store.update_artifact("operation-1", "shard-1", 39)
    with pytest.raises(ProgressConflictError, match="Stale or unknown"):
        resumed_store.update_artifact("old-operation", "shard-1", 41)

    resumed_store.update_artifact("operation-1", "shard-1", 100)
    completed = resumed_store.set_state("operation-1", TransferState.COMPLETE)
    assert completed.bytes_downloaded == completed.bytes_total


def test_progress_refuses_overlapping_active_operation(tmp_path: Path) -> None:
    store = PackProgressStore(tmp_path / "records")
    store.start(pack_id="fish-speech-1.4", revision="v1", artifacts={"weights": 4})

    with pytest.raises(ProgressConflictError, match="already active"):
        store.start(pack_id="fish-speech-1.4", revision="v1", artifacts={"weights": 4})


def test_performance_record_captures_required_resource_fields(tmp_path: Path) -> None:
    ticks = iter((0.0, 1.5, 2.0, 5.0, 6.25))
    recorder = ModelPerformanceRecorder(
        pack_id="moss-local-5b",
        revision="revision-2",
        accelerator=AcceleratorUsage(backend="metal", device_name="Apple GPU"),
        clock=lambda: next(ticks),
        sample_interval_seconds=60,
    )
    recorder.mark_model_ready()
    recorder.begin_render()
    recorder.end_render()
    record = recorder.finish_unload(
        rendered_audio_seconds=12,
        unload_started_at=5.5,
        unload_succeeded=True,
    )

    assert record.startup_duration_seconds == 1.5
    assert record.render_duration_seconds == 3
    assert record.render_seconds_per_audio_second == 0.25
    assert record.accelerator.backend == "metal"
    assert record.peak_memory_bytes > 0
    assert record.memory_before_load_bytes > 0
    assert record.memory_after_unload_bytes > 0
    assert record.available_memory_after_unload_bytes > 0
    assert record.unload_duration_seconds == 0.75
    assert record.unload_succeeded

    store = PerformanceRecordStore(tmp_path / "records")
    path = store.write(record)
    assert path.is_file()
    assert store.list() == (record,)


def test_managed_removal_requires_confirmation_and_is_recoverable(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    pack = managed / "moss-local-5b"
    pack.mkdir(parents=True)
    (pack / "weights.bin").write_bytes(b"model")
    trash = ManagedPackTrash(managed)

    with pytest.raises(ManagedTrashError, match="Explicit confirmation"):
        trash.move_to_trash("moss-local-5b", confirmation="yes")

    entry = trash.move_to_trash(
        "moss-local-5b",
        confirmation=ManagedPackTrash.remove_confirmation("moss-local-5b"),
    )
    assert not pack.exists()
    assert (trash.trash_root / entry.trash_id / "pack" / "weights.bin").read_bytes() == b"model"
    assert trash.list() == (entry,)

    with pytest.raises(ManagedTrashError, match="Explicit confirmation"):
        trash.restore(entry.trash_id, confirmation="restore")

    restored = trash.restore(
        entry.trash_id,
        confirmation=ManagedPackTrash.restore_confirmation(entry.trash_id),
    )
    assert restored == entry
    assert (pack / "weights.bin").read_bytes() == b"model"
    assert trash.list() == ()


def test_restore_never_overwrites_an_existing_pack(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    pack = managed / "fish-speech-1.4"
    pack.mkdir(parents=True)
    (pack / "original").write_text("original", encoding="utf-8")
    trash = ManagedPackTrash(managed)
    entry = trash.move_to_trash(
        "fish-speech-1.4",
        confirmation=ManagedPackTrash.remove_confirmation("fish-speech-1.4"),
    )
    pack.mkdir()
    (pack / "replacement").write_text("replacement", encoding="utf-8")

    with pytest.raises(ManagedTrashError, match="existing pack"):
        trash.restore(
            entry.trash_id,
            confirmation=ManagedPackTrash.restore_confirmation(entry.trash_id),
        )

    assert (pack / "replacement").read_text(encoding="utf-8") == "replacement"
    assert (trash.trash_root / entry.trash_id / "pack" / "original").is_file()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs elevated Windows privileges")
def test_managed_removal_refuses_symlink_pack(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    outside = tmp_path / "outside"
    outside.mkdir()
    managed.mkdir()
    (managed / "moss-8b").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManagedTrashError, match="unsafe"):
        ManagedPackTrash(managed).move_to_trash(
            "moss-8b",
            confirmation=ManagedPackTrash.remove_confirmation("moss-8b"),
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs elevated Windows privileges")
def test_managed_trash_refuses_symlinked_root_entry_manifest_and_payload(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    outside = tmp_path / "outside"
    outside.mkdir()
    managed.mkdir()
    (managed / ".trash").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManagedTrashError, match="symlink"):
        ManagedPackTrash(managed).list()

    (managed / ".trash").unlink()
    (managed / ".trash").mkdir()
    fake_entry = "moss-8b-fake"
    (managed / ".trash" / fake_entry).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManagedTrashError, match="symlink"):
        ManagedPackTrash(managed).restore(
            fake_entry,
            confirmation=ManagedPackTrash.restore_confirmation(fake_entry),
        )
    (managed / ".trash" / fake_entry).unlink()

    pack = managed / "moss-8b"
    pack.mkdir()
    (pack / "weights.bin").write_bytes(b"model")
    trash = ManagedPackTrash(managed)
    entry = trash.move_to_trash(
        "moss-8b", confirmation=ManagedPackTrash.remove_confirmation("moss-8b")
    )
    entry_directory = trash.trash_root / entry.trash_id
    payload = entry_directory / "pack"
    saved_payload = entry_directory / "saved-pack"
    payload.replace(saved_payload)
    payload.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManagedTrashError, match="symlink"):
        trash.restore(
            entry.trash_id,
            confirmation=ManagedPackTrash.restore_confirmation(entry.trash_id),
        )
    assert saved_payload.is_dir()

    payload.unlink()
    saved_payload.replace(payload)
    manifest = entry_directory / "trash.json"
    saved_manifest = entry_directory / "saved-trash.json"
    manifest.replace(saved_manifest)
    manifest.symlink_to(tmp_path / "outside-manifest.json")
    with pytest.raises(ManagedTrashError, match="symlink"):
        trash.restore(
            entry.trash_id,
            confirmation=ManagedPackTrash.restore_confirmation(entry.trash_id),
        )
    assert payload.is_dir()


def test_same_process_contention_is_also_refused(tmp_path: Path) -> None:
    first = HeavyweightModelSlot(tmp_path, "fish-speech-1.4")
    second = HeavyweightModelSlot(tmp_path, "moss-8b")
    first.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(HeavyweightModelSlotUnavailable):
            second.acquire(timeout_seconds=0.05)
        assert time.monotonic() - started < 1
    finally:
        first.release()


def test_managed_facade_installs_offline_verifies_and_restores(tmp_path: Path) -> None:
    service, offline = _managed_service(tmp_path)

    installed = service.install("test-pack", offline_directory=offline, allow_network=False)
    verified = service.verify("test-pack")

    assert installed.progress is not None
    assert installed.progress.state is TransferState.COMPLETE
    assert installed.state == "installed"
    assert verified.state == "installed"
    assert (
        service.models_root / "managed" / "model-packs" / "test-pack" / "files" / "model.bin"
    ).read_bytes() == b"tiny-model"

    removed = service.remove("test-pack", confirmed=True)
    assert removed.receipt_id is not None
    assert not (service.models_root / "managed" / "model-packs" / "test-pack").exists()
    restored = service.restore(removed.receipt_id)
    assert restored.pack_id == "test-pack"
    assert restored.state == "installed"


def test_managed_facade_never_claims_smoke_success_without_runner(tmp_path: Path) -> None:
    service, offline = _managed_service(tmp_path)
    service.install("test-pack", offline_directory=offline, allow_network=False)

    report = service.smoke_test("test-pack")

    assert report.state == "installed"
    assert "not ready" in report.message
    assert not (service.registry.records_directory("test-pack") / "smoke.json").exists()


def test_managed_facade_records_real_injected_offline_smoke(tmp_path: Path) -> None:
    service, offline = _managed_service(tmp_path)
    service.install("test-pack", offline_directory=offline, allow_network=False)
    service.smoke_runner = lambda _pack, _directory: SmokeResult(
        pcm_s16le=b"\xe8\x03" * 24_000,
        sample_rate=24_000,
        message="Fixed offline test rendered successfully.",
    )

    report = service.smoke_test("test-pack")

    assert report.state == "ready"
    assert (service.registry.records_directory("test-pack") / "runtime.json").is_file()
    assert (service.registry.records_directory("test-pack") / "smoke.json").is_file()


def test_managed_facade_refuses_to_overwrite_corrupt_managed_bytes(tmp_path: Path) -> None:
    service, offline = _managed_service(tmp_path)
    destination = (
        service.models_root / "managed" / "model-packs" / "test-pack" / "files" / "model.bin"
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"do-not-overwrite")

    with pytest.raises(ValueError, match="Refusing to overwrite"):
        service.install("test-pack", offline_directory=offline, allow_network=False)

    assert destination.read_bytes() == b"do-not-overwrite"


def test_pack_removal_refuses_shared_read_only_installation(tmp_path: Path) -> None:
    service, _offline = _managed_service(tmp_path, existing_directory="shared/test-pack")
    shared = service.models_root / "shared" / "test-pack"
    shared.mkdir(parents=True)
    (shared / "model.bin").write_bytes(b"tiny-model")

    with pytest.raises(ValueError, match="shared installation is read-only"):
        service.remove("test-pack", confirmed=True)

    assert (shared / "model.bin").read_bytes() == b"tiny-model"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs elevated Windows privileges")
def test_install_refuses_symlinked_destination_and_never_writes_outside_root(
    tmp_path: Path,
) -> None:
    service, offline = _managed_service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = service.models_root / "managed" / "model-packs" / "test-pack" / "files"
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        service.install("test-pack", offline_directory=offline, allow_network=False)

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs elevated Windows privileges")
def test_install_refuses_symlinked_partial_file(tmp_path: Path) -> None:
    service, offline = _managed_service(tmp_path)
    pack = service.registry.get("test-pack")
    revision_key = hashlib.sha256(pack.revision.encode("utf-8")).hexdigest()
    staging = (
        service.models_root / "managed" / ".downloads" / "model-packs" / "test-pack" / revision_key
    )
    staging.mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"untouched")
    (staging / "model.bin.part").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        service.install("test-pack", offline_directory=offline, allow_network=False)

    assert outside.read_bytes() == b"untouched"


def test_untrusted_revision_is_hashed_for_staging_path(tmp_path: Path) -> None:
    revision = "../../outside/collision"
    service, offline = _managed_service(tmp_path, revision=revision)

    service.install("test-pack", offline_directory=offline, allow_network=False)

    expected_key = hashlib.sha256(revision.encode("utf-8")).hexdigest()
    staging_parent = service.models_root / "managed" / ".downloads" / "model-packs" / "test-pack"
    assert [path.name for path in staging_parent.iterdir()] == [expected_key]
    assert not (service.models_root / "managed" / "outside").exists()


def test_full_sized_corrupt_partial_restarts_instead_of_requesting_past_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _offline = _managed_service(tmp_path, payload=b"good")
    destination = tmp_path / "model.bin.part"
    destination.write_bytes(b"bad!")
    requested_ranges: list[str | None] = []

    class Response:
        status = 200

        def __init__(self) -> None:
            self.remaining = b"good"

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int) -> bytes:
            chunk, self.remaining = self.remaining, b""
            return chunk

    def fake_urlopen(request: Any, timeout: int) -> Response:
        assert timeout == 60
        requested_ranges.append(request.headers.get("Range"))
        return Response()

    monkeypatch.setattr("whoopy.model_packs.manager.urllib.request.urlopen", fake_urlopen)
    progress: list[int] = []

    ManagedModelPacks._download_with_progress(
        service.registry.get("test-pack"),
        Path("model.bin"),
        destination,
        expected_size=4,
        progress=progress.append,
    )

    assert requested_ranges == ["bytes=0-"]
    assert destination.read_bytes() == b"good"
    assert progress == [4]
