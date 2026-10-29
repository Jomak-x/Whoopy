from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import whoopy.model_packs.runtime as runtime_module
from whoopy.model_packs import (
    ManagedRuntimePacks,
    ModelPackOperationError,
    RuntimeBundleSpec,
    RuntimePackManifest,
    RuntimePackSpec,
    RuntimePackState,
    load_runtime_pack_manifest,
)
from whoopy.model_packs.runtime import RuntimeBundleBuildRecord
from whoopy.model_packs.runtime_builder import (
    RuntimeBuildRecipe,
    build_runtime_bundle,
    bundle_manifest_snippet,
    load_runtime_build_recipes,
    lock_runtime_wheelhouse,
    validate_hashed_requirements,
)


def _archive(path: Path, files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as output:
        for name, contents in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            info.mode = 0o755 if name.endswith("python") else 0o644
            output.addfile(info, io.BytesIO(contents))
    value = payload.getvalue()
    path.write_bytes(value)
    return value


def _service(tmp_path: Path, archive: bytes) -> ManagedRuntimePacks:
    bundle = RuntimeBundleSpec(
        target="darwin-arm64",
        filename="runtime.tar.gz",
        size_bytes=len(archive),
        sha256=hashlib.sha256(archive).hexdigest(),
        archive="tar.gz",
    )
    spec = RuntimePackSpec(
        runtime_id="test-runtime",
        display_name="Test runtime",
        revision="runtime-1",
        upstream_repository="https://example.test/source",
        upstream_revision="a" * 40,
        python_version="3.12.1",
        license_id="Apache-2.0",
        managed_directory=Path("managed/runtimes/test-runtime"),
        required_markers=(Path(".venv/pyvenv.cfg"), Path(".venv/bin/python")),
        bundles=(bundle,),
    )
    return ManagedRuntimePacks(
        RuntimePackManifest(schema_version=1, runtimes=(spec,)), models_root=tmp_path
    )


def test_default_runtime_manifest_never_blesses_development_virtualenvs() -> None:
    manifest = load_runtime_pack_manifest(Path("config/runtime_packs.yaml"))

    assert {item.runtime_id for item in manifest.runtimes} == {
        "fish-speech",
        "moss-tts",
        "qwen3-tts",
    }
    fish = next(item for item in manifest.runtimes if item.runtime_id == "fish-speech")
    assert len(fish.bundles) == 1
    assert fish.bundles[0].install_mode == "locked-wheelhouse"
    assert all("experimental" not in str(item.managed_directory) for item in manifest.runtimes)
    qwen = next(item for item in manifest.runtimes if item.runtime_id == "qwen3-tts")
    assert qwen.upstream_revision == "022e286b98fbec7e1e916cb940cdf532cd9f488e"
    assert qwen.python_version == "3.12.12"
    assert len(qwen.bundles) == 1
    assert qwen.bundles[0].install_mode == "locked-wheelhouse"
    assert qwen.bundles[0].sha256 == (
        "90984ef46a8c879b98de8e33fbfacef841f31b09bbe5f79d61c68b977bbe67c4"
    )


def test_published_runtime_has_actionable_offline_install_state(tmp_path: Path) -> None:
    manifest = load_runtime_pack_manifest(Path("config/runtime_packs.yaml"))
    service = ManagedRuntimePacks(manifest, models_root=tmp_path)

    status = service.inspect("fish-speech", target="darwin-arm64")

    assert status.state is RuntimePackState.MISSING
    assert "pinned isolated runtime" in status.message.lower()
    assert "runtime install fish-speech" in str(status.remedy)
    assert "--offline-dir" in str(status.remedy)
    assert "Python-3.10.19".lower() in str(status.remedy).lower()
    with pytest.raises(ModelPackOperationError, match=r"unavailable.*--offline-dir"):
        service.install("fish-speech", target="darwin-arm64", allow_network=False)


def test_published_qwen_runtime_has_actionable_offline_install_state(tmp_path: Path) -> None:
    manifest = load_runtime_pack_manifest(Path("config/runtime_packs.yaml"))
    status = ManagedRuntimePacks(manifest, models_root=tmp_path).inspect(
        "qwen3-tts", target="darwin-arm64"
    )

    assert status.state is RuntimePackState.MISSING
    assert "pinned isolated runtime" in status.message.lower()
    assert "runtime install qwen3-tts" in str(status.remedy)
    assert "python-3.12.12" in str(status.remedy)


def test_offline_bundle_install_is_pinned_isolated_and_resumable(tmp_path: Path) -> None:
    offline = tmp_path / "offline"
    offline.mkdir()
    archive = _archive(
        offline / "runtime.tar.gz",
        {
            ".venv/pyvenv.cfg": b"version = 3.12.1\n",
            ".venv/bin/python": b"runtime-python",
        },
    )
    service = _service(tmp_path, archive)
    partial = tmp_path / "managed/.downloads/runtimes/test-runtime/runtime.tar.gz.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(archive[:23])

    assert service.inspect("test-runtime", target="darwin-arm64").state is RuntimePackState.PARTIAL
    status = service.install(
        "test-runtime",
        offline_directory=offline,
        allow_network=False,
        target="darwin-arm64",
    )

    assert status.state is RuntimePackState.INSTALLED
    assert (status.directory / ".venv/bin/python").read_bytes() == b"runtime-python"
    assert (status.directory / ".whoopy-runtime.json").is_file()
    assert (
        service.install(
            "test-runtime", offline_directory=offline, allow_network=False, target="darwin-arm64"
        )
        == status
    )
    (status.directory / ".venv/bin/python").write_bytes(b"tampered")
    assert service.inspect("test-runtime", target="darwin-arm64").state is RuntimePackState.CORRUPT


def test_runtime_bundle_digest_failure_never_promotes_files(tmp_path: Path) -> None:
    offline = tmp_path / "offline"
    offline.mkdir()
    archive = _archive(
        offline / "runtime.tar.gz",
        {".venv/pyvenv.cfg": b"config", ".venv/bin/python": b"python"},
    )
    service = _service(tmp_path, archive)
    (offline / "runtime.tar.gz").write_bytes(archive + b"tampered")

    with pytest.raises(ModelPackOperationError, match="size or SHA-256"):
        service.install(
            "test-runtime",
            offline_directory=offline,
            allow_network=False,
            target="darwin-arm64",
        )
    assert not (tmp_path / "managed/runtimes/test-runtime").exists()


def test_runtime_archive_rejects_path_traversal(tmp_path: Path) -> None:
    offline = tmp_path / "offline"
    offline.mkdir()
    archive = _archive(
        offline / "runtime.tar.gz",
        {
            ".venv/pyvenv.cfg": b"config",
            ".venv/bin/python": b"python",
            "../escape": b"bad",
        },
    )
    service = _service(tmp_path, archive)

    with pytest.raises(ModelPackOperationError, match="Unsafe runtime archive"):
        service.install(
            "test-runtime",
            offline_directory=offline,
            allow_network=False,
            target="darwin-arm64",
        )
    assert not (tmp_path / "escape").exists()


def _git(source: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize(
    ("target", "wheel_tag", "compatible"),
    [
        ("darwin-arm64", "py3-none-any", True),
        ("darwin-arm64", "cp312-cp312-macosx_14_0_arm64", True),
        ("darwin-arm64", "cp310-abi3-macosx_11_0_universal2", True),
        ("darwin-arm64", "cp312-cp312-manylinux_2_17_x86_64", False),
        ("darwin-arm64", "cp312-cp312-macosx_14_0_x86_64", False),
        ("darwin-arm64", "cp310-cp310-macosx_11_0_arm64", False),
        ("darwin-arm64", "cp313-abi3-macosx_14_0_arm64", False),
        ("darwin-arm64", "py2-none-any", False),
        ("linux-x86_64", "cp312-cp312-manylinux_2_17_x86_64", True),
        ("linux-x86_64", "cp312-cp312-macosx_14_0_arm64", False),
        ("linux-x86_64", "cp312-cp312-manylinux_2_17_aarch64", False),
    ],
)
def test_runtime_builder_snapshots_pinned_git_and_complete_hashed_wheelhouse(
    tmp_path: Path,
    target: str,
    wheel_tag: str,
    compatible: bool,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "runtime@example.test")
    _git(source, "config", "user.name", "Runtime Builder")
    (source / "pyproject.toml").write_text("[build-system]\nrequires=[]\n", "utf-8")
    package = source / "example_runtime"
    package.mkdir()
    (package / "__init__.py").write_text("VERSION = '1'\n", "utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "pinned source")
    revision = _git(source, "rev-parse", "HEAD")
    # Dirty and untracked development bytes are intentionally not bundled.
    (package / "__init__.py").write_text("DIRTY = True\n", "utf-8")
    (source / ".venv").mkdir()
    (source / ".venv/copied").write_text("never trust this", "utf-8")

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / f"example_dependency-1.0-{wheel_tag}.whl"
    wheel.write_bytes(b"reviewed-wheel")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = tmp_path / "requirements.lock"
    lock.write_text(f"example-dependency==1.0 --hash=sha256:{digest}\n", "utf-8")
    recipe = RuntimeBuildRecipe(
        runtime_id="example-runtime",
        revision=revision,
        upstream_repository="https://example.test/runtime",
        upstream_revision=revision,
        python_version="3.12.12",
        source_markers=(Path("pyproject.toml"), Path("example_runtime/__init__.py")),
        import_modules=("example_runtime",),
        supported_targets=("darwin-arm64", "linux-x86_64"),
    )

    if not compatible:
        with pytest.raises(ModelPackOperationError, match="incompatible"):
            build_runtime_bundle(
                recipe,
                source=source,
                requirements_lock=lock,
                wheelhouse=wheelhouse,
                output_directory=tmp_path / "first",
                target=target,
            )
        assert not (tmp_path / "first").exists()
        return

    first = build_runtime_bundle(
        recipe,
        source=source,
        requirements_lock=lock,
        wheelhouse=wheelhouse,
        output_directory=tmp_path / "first",
        target=target,
    )
    second = build_runtime_bundle(
        recipe,
        source=source,
        requirements_lock=lock,
        wheelhouse=wheelhouse,
        output_directory=tmp_path / "second",
        target=target,
    )

    assert first.sha256 == second.sha256
    assert first.size_bytes == second.size_bytes
    assert "install_mode: locked-wheelhouse" in bundle_manifest_snippet(first)
    with tarfile.open(first.archive, "r:gz") as archive:
        names = set(archive.getnames())
        assert ".venv/copied" not in names
        assert archive.extractfile("example_runtime/__init__.py").read() == b"VERSION = '1'\n"  # type: ignore[union-attr]
        assert "_whoopy_bundle/build.json" in names


def test_runtime_builder_rejects_floating_or_incomplete_dependencies(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    wheel = wheelhouse / "dependency-1-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    with pytest.raises(ModelPackOperationError, match="not exactly pinned"):
        validate_hashed_requirements(b"dependency>=1\n", wheelhouse)
    with pytest.raises(ModelPackOperationError, match=r"matching requirement|incomplete"):
        validate_hashed_requirements(b"dependency==1 --hash=sha256:" + b"a" * 64, wheelhouse)


def test_runtime_lock_binds_digest_to_matching_name_and_version(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    first = wheelhouse / "first_package-1.0-py3-none-any.whl"
    second = wheelhouse / "second_package-2.0-py3-none-any.whl"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_digest = hashlib.sha256(first.read_bytes()).hexdigest()
    second_digest = hashlib.sha256(second.read_bytes()).hexdigest()
    swapped = (
        f"first-package==1.0 --hash=sha256:{second_digest}\n"
        f"second-package==2.0 --hash=sha256:{first_digest}\n"
    ).encode()

    with pytest.raises(ModelPackOperationError, match="matching requirement"):
        validate_hashed_requirements(swapped, wheelhouse)


def test_runtime_wheelhouse_lock_is_deterministic_and_validated(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    (wheelhouse / "Example_Dependency-1.2.3-py3-none-any.whl").write_bytes(b"reviewed")

    lock = lock_runtime_wheelhouse(wheelhouse)

    assert lock.startswith(b"example-dependency==1.2.3 --hash=sha256:")
    assert validate_hashed_requirements(lock, wheelhouse) == {
        "Example_Dependency-1.2.3-py3-none-any.whl": hashlib.sha256(b"reviewed").hexdigest()
    }


def test_runtime_integrity_ignores_disposable_python_bytecode(tmp_path: Path) -> None:
    source = tmp_path / "runtime.py"
    source.write_text("VALUE = 1\n", "utf-8")
    bytecode = tmp_path / "__pycache__" / "runtime.cpython-310.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"first execution cache")

    before = runtime_module._tree_sha256(tmp_path)
    bytecode.write_bytes(b"later execution cache")

    assert runtime_module._tree_sha256(tmp_path) == before
    source.write_text("VALUE = 2\n", "utf-8")
    assert runtime_module._tree_sha256(tmp_path) != before


def test_checked_in_build_recipes_make_missing_release_inputs_explicit() -> None:
    recipes = load_runtime_build_recipes(Path("config/runtime_build_recipes.yaml"))

    assert {item.runtime_id for item in recipes.runtimes} == {
        "fish-speech",
        "moss-tts",
        "qwen3-tts",
    }
    assert all(item.upstream_revision == item.revision for item in recipes.runtimes)
    fish = next(item for item in recipes.runtimes if item.runtime_id == "fish-speech")
    moss = next(item for item in recipes.runtimes if item.runtime_id == "moss-tts")
    qwen = next(item for item in recipes.runtimes if item.runtime_id == "qwen3-tts")
    assert fish.lock_paths == {
        "darwin-arm64": Path("config/runtime-locks/fish-speech-darwin-arm64.txt")
    }
    assert moss.lock_paths == {
        "darwin-arm64": Path("config/runtime-locks/moss-tts-darwin-arm64.txt")
    }
    assert qwen.lock_paths == {
        "darwin-arm64": Path("config/runtime-locks/qwen3-tts-darwin-arm64.txt")
    }
    targets = {item.runtime_id: item.supported_targets for item in recipes.runtimes}
    assert targets == {
        "fish-speech": ("darwin-arm64",),
        "moss-tts": ("darwin-arm64", "linux-x86_64"),
        "qwen3-tts": ("darwin-arm64",),
    }


def test_runtime_recipe_rejects_unsafe_release_inputs() -> None:
    def recipe(
        *,
        import_modules: tuple[str, ...] = ("example",),
        supported_targets: tuple[str, ...] = ("darwin-arm64",),
        lock_paths: dict[str, Path] | None = None,
    ) -> RuntimeBuildRecipe:
        return RuntimeBuildRecipe(
            runtime_id="example",
            revision="a" * 40,
            upstream_repository="https://example.test/runtime",
            upstream_revision="a" * 40,
            python_version="3.12.12",
            source_markers=(Path("pyproject.toml"),),
            import_modules=import_modules,
            supported_targets=supported_targets,
            lock_paths=lock_paths or {},
        )

    with pytest.raises(ValueError, match="safe relative paths"):
        recipe(lock_paths={"darwin-arm64": Path("../outside.lock")})
    with pytest.raises(ValueError, match="dotted Python identifiers"):
        recipe(import_modules=("example; print('bad')",))
    with pytest.raises(ValueError, match="safe platform identifiers"):
        recipe(supported_targets=("../darwin-arm64",))
    with pytest.raises(ValueError, match="safe platform identifier"):
        RuntimeBundleSpec(
            target="../darwin-arm64",
            filename="example.tar.gz",
            size_bytes=1,
            sha256="b" * 64,
            archive="tar.gz",
        )


def test_locked_wheelhouse_install_without_builder_dependencies_creates_permanent_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = b"reviewed-wheel"
    wheel_digest = hashlib.sha256(wheel).hexdigest()
    lock = f"dependency==1 --hash=sha256:{wheel_digest}\n".encode()
    record = RuntimeBundleBuildRecord(
        runtime_id="test-runtime",
        revision="runtime-1",
        upstream_repository="https://example.test/source",
        upstream_revision="a" * 40,
        target="darwin-arm64",
        python_version="3.12.12",
        source_tree_sha256="b" * 64,
        requirements_sha256=hashlib.sha256(lock).hexdigest(),
        wheelhouse={"dependency-1-py3-none-any.whl": wheel_digest},
        import_modules=("example",),
    )
    archive_path = tmp_path / "runtime.tar.gz"
    archive = _archive(
        archive_path,
        {
            "example/__init__.py": b"VERSION = 1\n",
            "_whoopy_bundle/requirements.lock": lock,
            "_whoopy_bundle/wheelhouse/dependency-1-py3-none-any.whl": wheel,
            "_whoopy_bundle/build.json": record.model_dump_json().encode(),
        },
    )
    bundle = RuntimeBundleSpec(
        target="darwin-arm64",
        filename="runtime.tar.gz",
        size_bytes=len(archive),
        sha256=hashlib.sha256(archive).hexdigest(),
        archive="tar.gz",
        install_mode="locked-wheelhouse",
    )
    spec = RuntimePackSpec(
        runtime_id="test-runtime",
        display_name="Test runtime",
        revision="runtime-1",
        upstream_repository="https://example.test/source",
        upstream_revision="a" * 40,
        python_version="3.12.12",
        license_id="Apache-2.0",
        managed_directory=Path("managed/runtimes/test-runtime"),
        required_markers=(Path(".venv/pyvenv.cfg"), Path("example/__init__.py")),
        bundles=(bundle,),
    )
    service = ManagedRuntimePacks(
        RuntimePackManifest(schema_version=1, runtimes=(spec,)), models_root=tmp_path
    )
    destination = tmp_path / "managed/runtimes/test-runtime"
    destination.mkdir(parents=True)
    (destination / ".whoopy-installing.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "test-runtime",
                "revision": "runtime-1",
                "upstream_revision": "a" * 40,
                "target": "darwin-arm64",
                "bundle_sha256": bundle.sha256,
            }
        ),
        "utf-8",
    )
    (destination / "interrupted-junk").write_text("partial venv", "utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "platform.python_version" in command[-1]:
            return subprocess.CompletedProcess(command, 0, '{"version":"3.12.12"}\n', "")
        if command[1:3] == ["-m", "venv"]:
            venv = Path(command[3])
            (venv / "bin").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text("version = 3.12.12\n", "utf-8")
            (venv / "bin/python").write_text("runtime", "utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setitem(sys.modules, "whoopy.model_packs.runtime_builder", None)
    monkeypatch.setitem(sys.modules, "packaging", None)
    status = service.install(
        "test-runtime",
        offline_directory=tmp_path,
        allow_network=False,
        target="darwin-arm64",
        python_executable=Path("/reviewed/python3.12"),
    )

    venv_command = next(command for command in commands if command[1:3] == ["-m", "venv"])
    assert Path(venv_command[3]) == destination / ".venv"
    assert ".install-" not in venv_command[3]
    assert not (destination / "_whoopy_bundle").exists()
    assert not (destination / "interrupted-junk").exists()
    assert status.state is RuntimePackState.INSTALLED
    assert (
        service.inspect("test-runtime", target="darwin-arm64").state is RuntimePackState.INSTALLED
    )


def test_locked_runtime_recovers_interruption_immediately_after_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(
        tmp_path / "runtime.tar.gz",
        {".venv/pyvenv.cfg": b"version = 3.12.1\n", ".venv/bin/python": b"runtime"},
    )
    original = _service(tmp_path, archive).get("test-runtime")
    bundle = original.bundles[0].model_copy(update={"install_mode": "locked-wheelhouse"})
    spec = original.model_copy(update={"bundles": (bundle,)})
    service = ManagedRuntimePacks(
        RuntimePackManifest(schema_version=1, runtimes=(spec,)), models_root=tmp_path
    )
    destination = tmp_path / spec.managed_directory
    real_replace = Path.replace

    class InterruptedPromotion(BaseException):
        pass

    def interrupt_promotion(path: Path, target: Path) -> Path:
        result = real_replace(path, target)
        if target == destination:
            raise InterruptedPromotion
        return result

    with monkeypatch.context() as interruption:
        interruption.setattr(Path, "replace", interrupt_promotion)
        with pytest.raises(InterruptedPromotion):
            service.install(
                "test-runtime",
                offline_directory=tmp_path,
                allow_network=False,
                target="darwin-arm64",
            )

    intent = runtime_module.RuntimeInstallIntent.model_validate_json(
        (destination / ".whoopy-installing.json").read_text("utf-8")
    )
    assert intent.bundle_sha256 == bundle.sha256
    assert not (destination / ".whoopy-runtime.json").exists()
    monkeypatch.setattr(service, "_materialize_locked_runtime", lambda *_args, **_kwargs: None)

    status = service.install(
        "test-runtime",
        offline_directory=tmp_path,
        allow_network=False,
        target="darwin-arm64",
    )

    assert status.state is RuntimePackState.INSTALLED
    assert not (destination / ".whoopy-installing.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="directory fsync requires POSIX")
@pytest.mark.parametrize("interruption_stage", ["receipt_written", "receipt_published"])
def test_locked_runtime_receipt_publication_survives_process_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption_stage: str
) -> None:
    archive = _archive(
        tmp_path / "runtime.tar.gz",
        {".venv/pyvenv.cfg": b"version = 3.12.1\n", ".venv/bin/python": b"runtime"},
    )
    original = _service(tmp_path, archive).get("test-runtime")
    bundle = original.bundles[0].model_copy(update={"install_mode": "locked-wheelhouse"})
    spec = original.model_copy(update={"bundles": (bundle,)})
    manifest = RuntimePackManifest(schema_version=1, runtimes=(spec,))
    service = ManagedRuntimePacks(manifest, models_root=tmp_path)
    destination = tmp_path / spec.managed_directory
    source = """
import os
import sys
from pathlib import Path
from whoopy.model_packs.runtime import ManagedRuntimePacks, RuntimePackManifest

root = Path(sys.argv[1])
manifest = RuntimePackManifest.model_validate_json(sys.argv[2])
stage = sys.argv[3]
service = ManagedRuntimePacks(manifest, models_root=root)
service._materialize_locked_runtime = lambda *_args, **_kwargs: None
destination = root / manifest.runtimes[0].managed_directory
real_fsync = os.fsync

def interrupt_receipt(descriptor):
    real_fsync(descriptor)
    receipt = destination / ".whoopy-runtime.json"
    temporary = list(destination.glob("..whoopy-runtime.json.*.tmp"))
    if (stage == "receipt_written" and temporary) or (
        stage == "receipt_published" and receipt.exists()
    ):
        os._exit(17)

os.fsync = interrupt_receipt
service.install(
    "test-runtime", offline_directory=root, allow_network=False, target="darwin-arm64"
)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(sys.path)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(tmp_path),
            manifest.model_dump_json(),
            interruption_stage,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert child.returncode == 17, child.stderr
    assert (destination / ".whoopy-installing.json").is_file()
    receipt_path = destination / ".whoopy-runtime.json"
    assert receipt_path.exists() == (interruption_stage == "receipt_published")
    monkeypatch.setattr(service, "_materialize_locked_runtime", lambda *_args, **_kwargs: None)

    status = service.install(
        "test-runtime", offline_directory=tmp_path, allow_network=False, target="darwin-arm64"
    )

    assert status.state is RuntimePackState.INSTALLED
    receipt = runtime_module.RuntimeInstallReceipt.model_validate_json(
        receipt_path.read_text("utf-8")
    )
    assert receipt.bundle_sha256 == bundle.sha256
