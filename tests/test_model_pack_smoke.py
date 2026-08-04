from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest
import yaml

from whoopy.audio.models import PcmAudio
from whoopy.audio.synthesis import FatalSynthesisError, TransientSynthesisError
from whoopy.model_packs.manager import ManagedModelPacks, SmokeResult
from whoopy.model_packs.operations import PerformanceRecordStore
from whoopy.model_packs.registry import ModelPackRegistry, load_model_pack_registry
from whoopy.model_packs.smoke import OfflineModelPackSmokeRunner, _offline_environment


class _FakeAdapter:
    def __init__(self, events: list[str], *, close_fails: bool = False) -> None:
        self.events = events
        self.close_fails = close_fails

    def prepare(self) -> None:
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
        self.events.append("prepare")

    def synthesize(self, _segment: object) -> PcmAudio:
        assert os.environ["HF_DATASETS_OFFLINE"] == "1"
        self.events.append("synthesize")
        return PcmAudio(pcm_s16le=b"\xe8\x03" * 2_400, sample_rate=24_000)

    def close(self) -> None:
        self.events.append("close")
        if self.close_fails:
            raise RuntimeError("could not release runtime")


def _registry(
    tmp_path: Path,
    pack_ids: list[str],
    *,
    dependencies: dict[str, list[str]] | None = None,
) -> ModelPackRegistry:
    models = tmp_path / "models"
    payload = b"model"
    packs: list[dict[str, object]] = []
    for pack_id in pack_ids:
        packs.append(
            {
                "pack_id": pack_id,
                "display_name": pack_id,
                "revision": "pinned-revision",
                "source_repository": "https://huggingface.co/example/model",
                "license_id": "Apache-2.0",
                "license_url": "https://example.test/license",
                "commercial_use_allowed": True,
                "license_notice": "Test pack.",
                "managed_directory": f"managed/model-packs/{pack_id}/files",
                "supported_platforms": ["all"],
                "files": [
                    {
                        "path": "model.bin",
                        "size_bytes": len(payload),
                        "digest": {
                            "algorithm": "sha256",
                            "value": hashlib.sha256(payload).hexdigest(),
                        },
                        "role": "model",
                    }
                ],
                "runtime": {
                    "runtime_id": "test-runtime",
                    "revision": "runtime-revision",
                    "candidate_directories": [f"runtime/{pack_id}"],
                    "required_markers": ["python"],
                },
                "hardware": {
                    "min_total_ram_gb": 0.01,
                    "min_available_ram_gb": 0.01,
                    "min_free_disk_gb": 0,
                    "accelerator_any_of": ["cpu"],
                },
                "dependencies": (dependencies or {}).get(pack_id, []),
            }
        )
    path = tmp_path / "packs.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "packs": packs}), encoding="utf-8")
    return load_model_pack_registry(path, models_root=models)


def _installed_service(
    tmp_path: Path,
    runner: object,
) -> ManagedModelPacks:
    registry = _registry(tmp_path, ["kokoro"])
    files = registry.models_root / "managed/model-packs/kokoro/files"
    files.mkdir(parents=True)
    (files / "model.bin").write_bytes(b"model")
    runtime = registry.models_root / "runtime/kokoro"
    runtime.mkdir(parents=True)
    (runtime / "python").write_text("runtime", encoding="utf-8")
    return ManagedModelPacks(registry, smoke_runner=runner)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pack_id",
    [
        "kokoro",
        "fish-speech-1.4",
        "moss-audio-tokenizer-v2",
        "moss-local-5b",
        "moss-8b",
    ],
)
def test_offline_runner_dispatches_every_exact_pack_and_records_performance(
    tmp_path: Path,
    pack_id: str,
) -> None:
    registry = _registry(tmp_path, [pack_id])
    pack = registry.get(pack_id)
    events: list[str] = []
    before = {
        name: os.environ.get(name)
        for name in (
            "HF_HUB_OFFLINE",
            "TRANSFORMERS_OFFLINE",
            "HF_DATASETS_OFFLINE",
        )
    }
    runner = OfflineModelPackSmokeRunner(
        registry,
        adapter_factories={pack_id: lambda _pack, _path: _FakeAdapter(events)},
    )

    result = runner(pack, tmp_path / "selected")

    assert events == ["prepare", "synthesize", "close"]
    assert result.sample_rate == 24_000
    assert result.pcm_s16le == b"\xe8\x03" * 2_400
    assert "unloaded" in result.message
    assert {
        name: os.environ.get(name)
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")
    } == before
    records = PerformanceRecordStore(registry.records_directory(pack_id)).list()
    assert len(records) == 1
    assert records[0].pack_id == pack_id
    assert records[0].revision == "pinned-revision"
    assert records[0].rendered_audio_seconds == pytest.approx(0.1)
    assert records[0].unload_succeeded


def test_offline_runner_records_failed_unload_and_refuses_success(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ["moss-8b"])
    events: list[str] = []
    runner = OfflineModelPackSmokeRunner(
        registry,
        adapter_factories={"moss-8b": lambda _pack, _path: _FakeAdapter(events, close_fails=True)},
    )

    with pytest.raises(ValueError, match="did not unload cleanly"):
        runner(registry.get("moss-8b"), tmp_path / "selected")

    records = PerformanceRecordStore(registry.records_directory("moss-8b")).list()
    assert len(records) == 1
    assert not records[0].unload_succeeded


def test_production_facade_installs_real_offline_runner_by_default(tmp_path: Path) -> None:
    registry = _registry(tmp_path, ["kokoro"])
    manifest_path = tmp_path / "packs.yaml"

    service = ManagedModelPacks.from_paths(manifest_path, registry.models_root)

    assert isinstance(service.smoke_runner, OfflineModelPackSmokeRunner)


def test_offline_environment_changes_are_serialized_between_threads() -> None:
    first_entered = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with _offline_environment():
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second() -> None:
        assert first_entered.wait(timeout=2)
        second_attempted.set()
        with _offline_environment():
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=2)
    assert second_attempted.wait(timeout=2)
    assert not second_entered.is_set()
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)
    assert second_entered.is_set()


def test_transient_smoke_failure_does_not_persist_incompatible_evidence(
    tmp_path: Path,
) -> None:
    def transient(_pack: object, _path: Path) -> SmokeResult:
        raise TransientSynthesisError("heavyweight slot is busy")

    service = _installed_service(tmp_path, transient)

    with pytest.raises(ValueError, match="slot is busy"):
        service.smoke_test("kokoro")

    records = service.registry.records_directory("kokoro")
    assert not (records / "runtime.json").exists()
    assert not (records / "smoke.json").exists()
    assert service.registry.inspect("kokoro").state == "installed"


def test_deterministic_smoke_failure_can_persist_incompatible_evidence(tmp_path: Path) -> None:
    def fatal(_pack: object, _path: Path) -> SmokeResult:
        raise FatalSynthesisError("runtime import is incompatible")

    service = _installed_service(tmp_path, fatal)

    with pytest.raises(ValueError, match="incompatible"):
        service.smoke_test("kokoro")

    assert service.registry.inspect("kokoro").state == "incompatible"


@pytest.mark.parametrize(
    ("pcm", "message"),
    [
        (b"\x00\x00" * 2_400, "completely silent"),
        (b"\xff\x7f" * 2_400, "peak_dbfs"),
        (b"\x01", "divisible by two"),
    ],
)
def test_smoke_rejects_silent_clipped_or_invalid_pcm(
    tmp_path: Path,
    pcm: bytes,
    message: str,
) -> None:
    service = _installed_service(
        tmp_path,
        lambda _pack, _path: SmokeResult(
            pcm_s16le=pcm,
            sample_rate=24_000,
            message="must not be accepted",
        ),
    )

    with pytest.raises(ValueError, match=message):
        service.smoke_test("kokoro")

    assert service.registry.inspect("kokoro").state == "incompatible"
    assert not (service.registry.records_directory("kokoro") / "smoke.json").exists()


def test_parent_synthesis_can_record_dependency_paths_it_really_exercised(
    tmp_path: Path,
) -> None:
    registry = _registry(
        tmp_path,
        ["codec", "voice"],
        dependencies={"voice": ["codec"]},
    )
    for pack_id in ("codec", "voice"):
        files = registry.models_root / "managed" / "model-packs" / pack_id / "files"
        files.mkdir(parents=True)
        (files / "model.bin").write_bytes(b"model")
        runtime = registry.models_root / "runtime" / pack_id
        runtime.mkdir(parents=True)
        (runtime / "python").write_text("runtime", encoding="utf-8")
    service = ManagedModelPacks(
        registry,
        smoke_runner=lambda _pack, _path: SmokeResult(
            pcm_s16le=b"\xe8\x03" * 2_400,
            sample_rate=24_000,
            message="Voice and codec passed together.",
            validated_dependencies=("codec",),
        ),
    )

    report = service.smoke_test("voice")

    assert report.state == "ready"
    assert registry.inspect("codec").state == "ready"
