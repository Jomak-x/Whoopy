from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from pytest import MonkeyPatch

from whoopy.artifacts import (
    ArchiveFormat,
    ArtifactError,
    ArtifactInstaller,
    ArtifactLock,
    ArtifactProfile,
    ArtifactSpec,
    ArtifactState,
    ArtifactStore,
    TargetPlatform,
    _download_with_resume,
    load_artifact_lock,
)


def _artifact(
    artifact_id: str,
    component: str,
    payload: bytes,
    *,
    filename: str | None = None,
    archive: ArchiveFormat = ArchiveFormat.NONE,
    kind: Literal["model", "runtime", "tts_model", "python_wheel"] = "model",
) -> ArtifactSpec:
    return ArtifactSpec(
        artifact_id=artifact_id,
        component=component,
        display_name=artifact_id.replace("_", " "),
        version="test-v1",
        kind=kind,
        license_id="Apache-2.0",
        source_url=f"https://example.invalid/{filename or artifact_id}",
        filename=filename or f"{artifact_id}.bin",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        operating_systems=["linux"],
        architectures=["x86_64"],
        archive=archive,
    )


def _tar_bz2(member_name: str, payload: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:bz2") as archive:
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _tar_bz2_symlink(member_name: str, target: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:bz2") as archive:
        info = tarfile.TarInfo(member_name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        archive.addfile(info)
    return output.getvalue()


def _zip(member_name: str, payload: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w") as archive:
        archive.writestr(member_name, payload)
    return output.getvalue()


def test_repository_artifact_lock_resolves_every_current_desktop_target() -> None:
    artifact_lock = load_artifact_lock(Path("config/artifacts.yaml"))

    for target in (
        TargetPlatform(operating_system="darwin", architecture="arm64"),
        TargetPlatform(operating_system="darwin", architecture="x86_64"),
        TargetPlatform(operating_system="linux", architecture="arm64"),
        TargetPlatform(operating_system="linux", architecture="x86_64"),
        TargetPlatform(operating_system="windows", architecture="x86_64"),
    ):
        basic = artifact_lock.resolve("basic", target)
        standard = artifact_lock.resolve("standard", target)

        assert [artifact.component for artifact in basic] == [
            "tts_model",
            "sherpa_onnx_python",
            "sherpa_onnx_core",
        ]
        assert len(standard) == 5


def test_platform_without_complete_native_stack_is_refused() -> None:
    artifact_lock = load_artifact_lock(Path("config/artifacts.yaml"))
    target = TargetPlatform(operating_system="windows", architecture="arm64")

    with pytest.raises(ArtifactError, match="sherpa_onnx_python"):
        artifact_lock.resolve("basic", target)


def test_invalid_lock_is_rejected_with_a_readable_error(tmp_path: Path) -> None:
    lock_path = tmp_path / "artifacts.yaml"
    lock_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "artifacts": [
                    {
                        "artifact_id": "unsafe",
                        "component": "model",
                        "display_name": "Unsafe",
                        "version": "1",
                        "kind": "model",
                        "license_id": "Apache-2.0",
                        "source_url": "http://example.test/model",
                        "filename": "../model.gguf",
                        "size_bytes": 1,
                        "sha256": "0" * 64,
                        "operating_systems": ["all"],
                        "architectures": ["all"],
                    }
                ],
                "profiles": {"basic": {"components": ["model"]}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactError, match="Invalid artifact lock"):
        load_artifact_lock(lock_path)


def test_offline_install_verifies_extracts_and_is_idempotent(tmp_path: Path) -> None:
    model_payload = b"tiny deterministic model"
    archive_payload = _tar_bz2("kokoro/model.onnx", b"tiny voice")
    model = _artifact("model", "llm_model", model_payload)
    voice = _artifact(
        "voice",
        "tts_model",
        archive_payload,
        filename="voice.tar.bz2",
        archive=ArchiveFormat.TAR_BZ2,
    )
    target = TargetPlatform(operating_system="linux", architecture="x86_64")
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / model.filename).write_bytes(model_payload)
    (offline / voice.filename).write_bytes(archive_payload)
    store = ArtifactStore(tmp_path / "managed")
    installer = ArtifactInstaller(store)

    first = installer.install_profile(
        "standard",
        [model, voice],
        target,
        offline_directory=offline,
        allow_network=False,
    )
    second = installer.install_profile(
        "standard",
        [model, voice],
        target,
        offline_directory=offline,
        allow_network=False,
    )

    assert first.installed == ["model", "voice"]
    assert second.reused == ["model", "voice"]
    assert store.inspect(model).state is ArtifactState.INSTALLED
    assert store.inspect(voice).state is ArtifactState.INSTALLED
    assert (store.installed / "voice" / "kokoro" / "model.onnx").read_bytes() == b"tiny voice"


def test_corrupt_offline_artifact_is_quarantined_and_rejected(tmp_path: Path) -> None:
    expected_payload = b"expected"
    artifact = _artifact("model", "llm_model", expected_payload)
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / artifact.filename).write_bytes(b"tampered")
    store = ArtifactStore(tmp_path / "managed")

    with pytest.raises(ArtifactError, match="failed SHA-256"):
        ArtifactInstaller(store).install_profile(
            "standard",
            [artifact],
            TargetPlatform(operating_system="linux", architecture="x86_64"),
            offline_directory=offline,
            allow_network=False,
        )

    assert not store.download_path(artifact).exists()
    assert list(store.quarantine.iterdir())


def test_full_verification_detects_changed_extracted_content(tmp_path: Path) -> None:
    archive_payload = _tar_bz2("kokoro/model.onnx", b"trusted voice")
    artifact = _artifact(
        "voice",
        "tts_model",
        archive_payload,
        filename="voice.tar.bz2",
        archive=ArchiveFormat.TAR_BZ2,
    )
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / artifact.filename).write_bytes(archive_payload)
    store = ArtifactStore(tmp_path / "managed")
    ArtifactInstaller(store).install_profile(
        "basic",
        [artifact],
        TargetPlatform(operating_system="linux", architecture="x86_64"),
        offline_directory=offline,
        allow_network=False,
    )

    (store.installed / "voice" / "kokoro" / "model.onnx").write_bytes(b"changed voice")

    status = store.inspect(artifact, verify_digest=True)
    assert status.state is ArtifactState.CORRUPT
    assert "extracted installation digest" in status.message


def test_verified_python_wheels_materialize_into_one_environment(tmp_path: Path) -> None:
    first_payload = _zip("sherpa_onnx/__init__.py", b"version = 'test'")
    second_payload = _zip("sherpa_onnx/lib/native.so", b"native")
    first = _artifact(
        "python",
        "sherpa_python",
        first_payload,
        filename="python.whl",
        kind="python_wheel",
    )
    second = _artifact(
        "native",
        "sherpa_native",
        second_payload,
        filename="native.whl",
        kind="python_wheel",
    )
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / first.filename).write_bytes(first_payload)
    (offline / second.filename).write_bytes(second_payload)
    store = ArtifactStore(tmp_path / "managed")
    ArtifactInstaller(store).install_profile(
        "basic",
        [first, second],
        TargetPlatform(operating_system="linux", architecture="x86_64"),
        offline_directory=offline,
        allow_network=False,
    )

    environment = store.materialize_python_wheels(
        [first, second],
        environment_name="sherpa_test",
    )
    reused = store.materialize_python_wheels(
        [first, second],
        environment_name="sherpa_test",
    )

    assert reused == environment
    assert (environment / "sherpa_onnx" / "__init__.py").is_file()
    assert (environment / "sherpa_onnx" / "lib" / "native.so").is_file()


def test_archive_path_traversal_is_rejected(tmp_path: Path) -> None:
    archive_payload = _tar_bz2("../escaped.txt", b"not allowed")
    artifact = _artifact(
        "unsafe_archive",
        "tts_model",
        archive_payload,
        filename="unsafe.tar.bz2",
        archive=ArchiveFormat.TAR_BZ2,
    )
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / artifact.filename).write_bytes(archive_payload)

    with pytest.raises(ArtifactError, match="Unsafe archive path"):
        ArtifactInstaller(ArtifactStore(tmp_path / "managed")).install_profile(
            "basic",
            [artifact],
            TargetPlatform(operating_system="linux", architecture="x86_64"),
            offline_directory=offline,
            allow_network=False,
        )
    assert not (tmp_path / "escaped.txt").exists()


def test_archive_link_that_escapes_install_directory_is_rejected(tmp_path: Path) -> None:
    archive_payload = _tar_bz2_symlink("runtime/library.so", "../../escaped.so")
    artifact = _artifact(
        "unsafe_link",
        "runtime",
        archive_payload,
        filename="unsafe-link.tar.bz2",
        archive=ArchiveFormat.TAR_BZ2,
    )
    offline = tmp_path / "offline"
    offline.mkdir()
    (offline / artifact.filename).write_bytes(archive_payload)

    with pytest.raises(ArtifactError, match="Unsafe archive link"):
        ArtifactInstaller(ArtifactStore(tmp_path / "managed")).install_profile(
            "standard",
            [artifact],
            TargetPlatform(operating_system="linux", architecture="x86_64"),
            offline_directory=offline,
            allow_network=False,
        )


class _FakeHttpResponse:
    def __init__(self, payload: bytes, *, status: int) -> None:
        self._stream = io.BytesIO(payload)
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self._stream.close()


def test_http_download_resumes_an_existing_partial(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    payload = b"abcdef"
    artifact = _artifact("model", "llm_model", payload)
    partial = tmp_path / "model.part"
    partial.write_bytes(payload[:3])
    captured_range: list[str | None] = []

    def fake_urlopen(request: urllib.request.Request, **_kwargs: Any) -> _FakeHttpResponse:
        captured_range.append(request.get_header("Range"))
        return _FakeHttpResponse(payload[3:], status=206)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    _download_with_resume(artifact, partial, timeout_seconds=1)

    assert captured_range == ["bytes=3-"]
    assert partial.read_bytes() == payload


def test_lock_model_rejects_ambiguous_component_resolution() -> None:
    payload = b"x"
    first = _artifact("first", "runtime", payload)
    second = _artifact("second", "runtime", payload)
    artifact_lock = ArtifactLock(
        schema_version=1,
        artifacts=[first, second],
        profiles={"standard": ArtifactProfile(components=["runtime"])},
    )

    with pytest.raises(ArtifactError, match="ambiguous"):
        artifact_lock.resolve(
            "standard",
            TargetPlatform(operating_system="linux", architecture="x86_64"),
        )
