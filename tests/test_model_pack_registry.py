from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from whoopy.hardware import HardwareSnapshot
from whoopy.model_packs import (
    DigestSpec,
    FileRole,
    HardwareRequirement,
    MachineIdentity,
    ModelPackError,
    ModelPackManifest,
    ModelPackRegistry,
    ModelPackSpec,
    ModelPackState,
    PinnedFileSpec,
    RuntimeEvidence,
    RuntimeSpec,
    ShardIndexSpec,
    SmokeTestEvidence,
    load_model_pack_registry,
)

NOW = datetime(2026, 8, 4, tzinfo=UTC)
MACHINE = MachineIdentity(machine_id="a" * 64, operating_system="darwin", architecture="arm64")


def _hardware(*, total: float = 64, available: float = 48, disk: float = 100) -> HardwareSnapshot:
    return HardwareSnapshot(
        operating_system="darwin",
        architecture="arm64",
        cpu_count=10,
        total_ram_gb=total,
        available_ram_gb=available,
        free_disk_gb=disk,
        accelerators=["cpu", "metal"],
    )


def _sha256(payload: bytes) -> DigestSpec:
    return DigestSpec(value=hashlib.sha256(payload).hexdigest())


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        if path.is_dir():
            digest.update(b"directory\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"file\0" + relative + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _pack(
    payloads: dict[str, bytes],
    *,
    pack_id: str = "test-pack",
    existing_directories: list[Path] | None = None,
    shard_index: ShardIndexSpec | None = None,
    dependencies: list[str] | None = None,
) -> ModelPackSpec:
    files = [
        PinnedFileSpec(
            path=Path(name),
            size_bytes=len(payload),
            digest=_sha256(payload),
            role=(FileRole.SHARD_INDEX if name.endswith("index.json") else FileRole.MODEL),
        )
        for name, payload in payloads.items()
    ]
    return ModelPackSpec(
        pack_id=pack_id,
        display_name=pack_id,
        revision="revision-1",
        source_repository="https://example.test/model",
        license_id="Apache-2.0",
        license_url="https://example.test/license",
        commercial_use_allowed=True,
        license_notice="Test license.",
        managed_directory=Path("managed/model-packs") / pack_id / "files",
        existing_directories=existing_directories or [],
        supported_platforms=["darwin-arm64"],
        files=files,
        shard_indexes=[] if shard_index is None else [shard_index],
        runtime=RuntimeSpec(
            runtime_id="test-runtime",
            revision="runtime-1",
            candidate_directories=[Path("runtimes/test")],
            required_markers=[Path("pyvenv.cfg"), Path("worker.py")],
        ),
        hardware=HardwareRequirement(
            min_total_ram_gb=8,
            min_available_ram_gb=4,
            min_free_disk_gb=2,
            accelerator_any_of=["metal"],
        ),
        dependencies=dependencies or [],
    )


def _registry(root: Path, *packs: ModelPackSpec) -> ModelPackRegistry:
    return ModelPackRegistry(ModelPackManifest(schema_version=1, packs=list(packs)), root)


def _write_pack(root: Path, pack: ModelPackSpec, payloads: dict[str, bytes]) -> Path:
    directory = root / pack.managed_directory
    for relative, payload in payloads.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return directory


def _write_runtime(root: Path) -> None:
    runtime = root / "runtimes/test"
    runtime.mkdir(parents=True)
    (runtime / "pyvenv.cfg").write_text("isolated=true\n", encoding="utf-8")
    (runtime / "worker.py").write_text("# offline worker\n", encoding="utf-8")


def _write_evidence(
    registry: ModelPackRegistry,
    pack: ModelPackSpec,
    *,
    runtime_success: bool = True,
    smoke_success: bool = True,
    machine_id: str = MACHINE.machine_id,
) -> None:
    records = registry.records_directory(pack.pack_id)
    records.mkdir(parents=True, exist_ok=True)
    runtime_fingerprint = registry.runtime_fingerprint(pack)
    assert runtime_fingerprint is not None
    runtime = RuntimeEvidence(
        pack_id=pack.pack_id,
        model_revision=pack.revision,
        runtime_revision=pack.runtime.revision,
        runtime_fingerprint=runtime_fingerprint,
        machine_id=machine_id,
        checked_at=NOW,
        success=runtime_success,
        message="runtime worked" if runtime_success else "runtime import failed",
    )
    smoke = SmokeTestEvidence(
        pack_id=pack.pack_id,
        model_revision=pack.revision,
        runtime_revision=pack.runtime.revision,
        runtime_fingerprint=runtime_fingerprint,
        machine_id=machine_id,
        checked_at=NOW,
        success=smoke_success,
        offline=True,
        output_sha256="b" * 64 if smoke_success else None,
        output_duration_seconds=1.0 if smoke_success else None,
        message="offline synthesis worked" if smoke_success else "synthesis crashed",
    )
    (records / "runtime.json").write_text(runtime.model_dump_json(), encoding="utf-8")
    (records / "smoke.json").write_text(smoke.model_dump_json(), encoding="utf-8")


def test_missing_partial_and_corrupt_states_are_distinct(tmp_path: Path) -> None:
    payloads = {"model.bin": b"model", "tokenizer.bin": b"tokenizer"}
    pack = _pack(payloads)
    registry = _registry(tmp_path, pack)

    assert registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE).state == "missing"

    directory = tmp_path / pack.managed_directory
    directory.mkdir(parents=True)
    (directory / "model.bin.incomplete").write_bytes(b"model")
    # Temporary downloader files do not masquerade as final pinned files.
    assert registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE).state == "missing"

    (directory / "model.bin").write_bytes(b"model")
    assert registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE).state == "partial"

    (directory / "tokenizer.bin").write_bytes(b"short")
    status = registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE)
    assert status.state == ModelPackState.CORRUPT
    assert "size mismatch" in status.files[1].message

    (directory / "tokenizer.bin").write_bytes(b"tokenizeq")
    status = registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE)
    assert status.state == ModelPackState.CORRUPT
    assert "sha256 mismatch" in status.files[1].message


def test_existing_files_are_inspected_in_place_without_mutation(tmp_path: Path) -> None:
    payloads = {"model.bin": b"model"}
    existing = Path("experimental/old-model")
    pack = _pack(payloads, existing_directories=[existing])
    old_directory = tmp_path / existing
    old_directory.mkdir(parents=True)
    model = old_directory / "model.bin"
    model.write_bytes(payloads["model.bin"])
    before = model.stat()

    status = _registry(tmp_path, pack).inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE)

    assert status.state == ModelPackState.INSTALLED
    assert status.selected_directory == old_directory
    assert model.read_bytes() == payloads["model.bin"]
    assert model.stat().st_ino == before.st_ino
    assert not (tmp_path / pack.managed_directory).exists()


def test_valid_shard_index_must_exactly_match_pinned_shards(tmp_path: Path) -> None:
    index_payload = json.dumps(
        {
            "metadata": {"total_size": 6},
            "weight_map": {"layer.0": "one.bin", "layer.1": "two.bin"},
        }
    ).encode()
    payloads = {"model.index.json": index_payload, "one.bin": b"one", "two.bin": b"two"}
    index = ShardIndexSpec(
        path=Path("model.index.json"), shard_paths=[Path("one.bin"), Path("two.bin")]
    )
    pack = _pack(payloads, shard_index=index)
    _write_pack(tmp_path, pack, payloads)
    registry = _registry(tmp_path, pack)

    status = registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE)
    assert status.state == ModelPackState.INSTALLED
    assert next(check for check in status.checks if check.check == "shard_indexes").passed

    bad_payload = json.dumps(
        {
            "metadata": {"total_size": 6},
            "weight_map": {"layer.0": "one.bin", "layer.1": "unexpected.bin"},
        }
    ).encode()
    bad_payloads = {**payloads, "model.index.json": bad_payload}
    bad_pack = _pack(bad_payloads, pack_id="bad-index", shard_index=index)
    _write_pack(tmp_path, bad_pack, bad_payloads)
    bad = _registry(tmp_path, bad_pack).inspect(
        bad_pack.pack_id, hardware=_hardware(), machine=MACHINE
    )
    assert bad.state == ModelPackState.CORRUPT
    assert "does not map exactly" in bad.checks[-1].message


def test_ready_requires_runtime_hardware_and_machine_scoped_offline_smoke(tmp_path: Path) -> None:
    payloads = {"model.bin": b"model"}
    pack = _pack(payloads)
    registry = _registry(tmp_path, pack)
    _write_pack(tmp_path, pack, payloads)
    _write_runtime(tmp_path)

    assert (
        registry.inspect(pack.pack_id, hardware=_hardware(total=4), machine=MACHINE).state
        == ModelPackState.RESOURCE_BLOCKED
    )
    assert (
        registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE).state
        == ModelPackState.INSTALLED
    )

    _write_evidence(registry, pack, machine_id="c" * 64)
    assert (
        registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE).state
        == ModelPackState.INSTALLED
    )

    _write_evidence(registry, pack)
    status = registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE)
    assert status.state == ModelPackState.READY
    assert all(check.passed for check in status.checks)

    # A cheap list can be responsive, but skipped hashes can never claim READY.
    cheap = registry.inspect(
        pack.pack_id, hardware=_hardware(), machine=MACHINE, verify_digests=False
    )
    assert cheap.state == ModelPackState.INSTALLED
    assert cheap.checks[-1].check == "full_digest_verification"


def test_ready_evidence_is_invalidated_when_runtime_marker_changes(tmp_path: Path) -> None:
    payloads = {"model.bin": b"model"}
    pack = _pack(payloads)
    _write_pack(tmp_path, pack, payloads)
    _write_runtime(tmp_path)
    registry = _registry(tmp_path, pack)
    _write_evidence(registry, pack)

    assert registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE).state == "ready"

    (tmp_path / "runtimes/test/worker.py").write_text("# locally edited worker\n", encoding="utf-8")
    status = registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE)

    assert status.state == ModelPackState.INSTALLED
    runtime_check = next(check for check in status.checks if check.check == "runtime_probe")
    assert not runtime_check.passed
    assert "runtime fingerprint" in runtime_check.message


def test_ready_evidence_is_invalidated_when_installed_packages_change(tmp_path: Path) -> None:
    payloads = {"model.bin": b"model"}
    pack = _pack(payloads)
    _write_pack(tmp_path, pack, payloads)
    _write_runtime(tmp_path)
    metadata = (
        tmp_path / "runtimes/test/.venv/lib/python3.12/site-packages/example-1.dist-info/METADATA"
    )
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: example\nVersion: 1\n", encoding="utf-8")
    registry = _registry(tmp_path, pack)
    _write_evidence(registry, pack)

    metadata.write_text("Name: example\nVersion: 2\n", encoding="utf-8")

    assert (
        registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE).state
        == ModelPackState.INSTALLED
    )


def test_failed_current_runtime_or_smoke_marks_pack_incompatible(tmp_path: Path) -> None:
    payloads = {"model.bin": b"model"}
    pack = _pack(payloads)
    registry = _registry(tmp_path, pack)
    _write_pack(tmp_path, pack, payloads)
    _write_runtime(tmp_path)

    _write_evidence(registry, pack, runtime_success=False, smoke_success=False)
    runtime_failure = registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE)
    assert runtime_failure.state == ModelPackState.INCOMPATIBLE
    assert "runtime import failed" in runtime_failure.checks[-1].message

    _write_evidence(registry, pack, smoke_success=False)
    smoke_failure = registry.inspect(pack.pack_id, hardware=_hardware(), machine=MACHINE)
    assert smoke_failure.state == ModelPackState.INCOMPATIBLE
    assert "synthesis crashed" in smoke_failure.checks[-1].message


def test_dependencies_must_be_ready_before_dependent_pack(tmp_path: Path) -> None:
    dependency = _pack({"codec.bin": b"codec"}, pack_id="codec")
    voice = _pack({"voice.bin": b"voice"}, pack_id="voice", dependencies=["codec"])
    registry = _registry(tmp_path, dependency, voice)
    _write_runtime(tmp_path)
    for pack, payloads in [(dependency, {"codec.bin": b"codec"}), (voice, {"voice.bin": b"voice"})]:
        _write_pack(tmp_path, pack, payloads)
        _write_evidence(registry, pack)

    assert registry.inspect("voice", hardware=_hardware(), machine=MACHINE).state == "ready"

    (registry.records_directory("codec") / "smoke.json").unlink()
    status = registry.inspect("voice", hardware=_hardware(), machine=MACHINE)
    assert status.state == ModelPackState.INSTALLED
    assert "dependency is installed" in status.checks[-1].message


def test_manifest_rejects_indirect_dependency_cycles() -> None:
    first = _pack({"a.bin": b"a"}, pack_id="first", dependencies=["second"])
    second = _pack({"b.bin": b"b"}, pack_id="second", dependencies=["third"])
    third = _pack({"c.bin": b"c"}, pack_id="third", dependencies=["first"])

    with pytest.raises(ValidationError, match=r"first -> second -> third -> first"):
        ModelPackManifest(schema_version=1, packs=[first, second, third])


def test_complete_tree_digest_detects_missing_unlisted_kokoro_resource(
    tmp_path: Path,
) -> None:
    payloads = {"model.onnx": b"model"}
    initial = _pack(payloads, pack_id="kokoro")
    directory = _write_pack(tmp_path, initial, payloads)
    lexicon = directory / "espeak-ng-data" / "en_dict"
    lexicon.parent.mkdir(parents=True)
    lexicon.write_bytes(b"pronunciation data")
    pack = initial.model_copy(update={"installed_tree_sha256": _tree_sha256(directory)})
    registry = _registry(tmp_path, pack)

    before = registry.inspect("kokoro", hardware=_hardware(), machine=MACHINE)
    assert before.state is ModelPackState.INSTALLED
    assert next(check for check in before.checks if check.check == "installed_tree").passed

    lexicon.unlink()
    after = registry.inspect("kokoro", hardware=_hardware(), machine=MACHINE)
    assert after.state is ModelPackState.CORRUPT
    assert not next(check for check in after.checks if check.check == "installed_tree").passed


def test_manifest_rejects_unsafe_paths_and_unknown_dependencies() -> None:
    with pytest.raises(ValidationError, match="safe relative path"):
        PinnedFileSpec(
            path=Path("../outside"),
            size_bytes=1,
            digest=DigestSpec(value="0" * 64),
            role=FileRole.MODEL,
        )

    pack = _pack({"model.bin": b"model"}, dependencies=["unknown"])
    with pytest.raises(ValidationError, match="unknown dependencies"):
        ModelPackManifest(schema_version=1, packs=[pack])


def test_default_registry_contains_all_pr14_packs_and_fish_license() -> None:
    registry = load_model_pack_registry()

    assert {pack.pack_id for pack in registry.packs} == {
        "kokoro",
        "fish-speech-1.4",
        "moss-audio-tokenizer-v2",
        "moss-local-5b",
        "moss-8b",
    }
    fish = registry.get("fish-speech-1.4")
    assert fish.license_id == "CC-BY-NC-SA-4.0"
    assert not fish.commercial_use_allowed
    assert "Non-commercial" in fish.license_notice
    kokoro = registry.get("kokoro")
    assert kokoro.installed_tree_sha256 == (
        "9bc58f2791c964568c1f9105a7272e4b73e7db113b141d204c8846e6796ed0af"
    )
    assert kokoro.installed_tree_directory == Path("managed/installed/kokoro_multilang_v1_0")

    with pytest.raises(ModelPackError, match="Unknown model pack"):
        registry.get("not-real")
