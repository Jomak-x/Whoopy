from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.build_app_offline_bundle import BUNDLE_GUIDES, _deterministic_zip, build_bundle
from scripts.install_offline_bundle import (
    _offline_environment,
    _target_id,
    install_bundle,
    verify_bundle,
)
from scripts.offline_bundle_acceptance import (
    MACOS_SANDBOX_ACTIVE,
    MACOS_SANDBOX_CONTROL,
    MACOS_SANDBOX_PROFILE,
    _clone_models_for_lifecycle,
    _readiness_tree_sha256,
    _verify_macos_sandbox_isolation,
    run_acceptance,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    (root / "wheelhouse").mkdir(parents=True)
    files = {
        "requirements.txt": b"demo==1 \\\n    --hash=sha256:" + b"a" * 64 + b"\n",
        "install.py": b"# installer\n",
        "wheelhouse/whoopy-0.1.0-py3-none-any.whl": b"product wheel",
        "wheelhouse/demo-1-py3-none-any.whl": b"dependency wheel",
    }
    entries: list[dict[str, object]] = []
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        entries.append({"path": relative, "size": len(payload), "sha256": _sha256(path)})
    entries.sort(key=lambda entry: str(entry["path"]))
    identity = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "evidence_scope": "whoopy_application_offline_bundle",
        "bundle_id": identity,
        "product_version": "0.1.0",
        "target": _target_id(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "lock_sha256": "b" * 64,
        "product_wheel": "wheelhouse/whoopy-0.1.0-py3-none-any.whl",
        "product_wheel_sha256": entries[2]["sha256"],
        "files": entries,
    }
    # Locate by path rather than relying on entry order in the fixture.
    manifest["product_wheel_sha256"] = next(
        entry["sha256"] for entry in entries if entry["path"] == manifest["product_wheel"]
    )
    (root / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return root


def test_bundle_verification_covers_complete_inventory_and_content_identity(tmp_path: Path) -> None:
    root = _bundle(tmp_path)

    manifest = verify_bundle(root)

    assert manifest["target"] == _target_id()
    assert manifest["product_wheel_sha256"] == _sha256(
        root / "wheelhouse/whoopy-0.1.0-py3-none-any.whl"
    )


def test_lifecycle_model_clone_hardlinks_payload_but_copies_mutable_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "workspace/models/managed"
    payload = source / "model-packs/kokoro/files/model.onnx"
    evidence = source / "model-packs/kokoro/records/smoke.json"
    payload.parent.mkdir(parents=True)
    evidence.parent.mkdir(parents=True)
    payload.write_bytes(b"immutable model")
    evidence.write_text('{"state":"ready"}', encoding="utf-8")
    isolated_root = tmp_path / "isolated"
    isolated_root.mkdir()

    destination = _clone_models_for_lifecycle(
        source,
        isolated_root / "product-data/models/managed",
        isolation_root=isolated_root,
    )
    cloned_payload = destination / payload.relative_to(source)
    cloned_evidence = destination / evidence.relative_to(source)

    assert cloned_payload.stat().st_ino == payload.stat().st_ino
    assert cloned_evidence.stat().st_ino != evidence.stat().st_ino
    before = _readiness_tree_sha256(source)
    cloned_evidence.write_text('{"state":"stale"}', encoding="utf-8")
    assert evidence.read_text("utf-8") == '{"state":"ready"}'
    assert _readiness_tree_sha256(source) == before


def test_lifecycle_model_clone_rejects_unsafe_destination_and_symlink(tmp_path: Path) -> None:
    source = tmp_path / "workspace/models/managed"
    source.mkdir(parents=True)
    (source / "outside").symlink_to(tmp_path / "outside")
    isolated_root = tmp_path / "isolated"
    isolated_root.mkdir()

    with pytest.raises(ValueError, match="below the lifecycle root"):
        _clone_models_for_lifecycle(
            source,
            tmp_path / "not-isolated/models",
            isolation_root=isolated_root,
        )
    with pytest.raises(ValueError, match="absolute symlink"):
        _clone_models_for_lifecycle(
            source,
            isolated_root / "models",
            isolation_root=isolated_root,
        )
    existing = isolated_root / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="new child directory"):
        _clone_models_for_lifecycle(source, existing, isolation_root=isolated_root)


def test_lifecycle_model_clone_skips_optional_runtime_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "workspace/models/managed"
    readiness = source / "model-packs/kokoro/records/smoke.json"
    readiness.parent.mkdir(parents=True)
    readiness.write_text('{"state":"ready"}', encoding="utf-8")
    runtime = source / "runtimes/fish/.venv/bin"
    runtime.mkdir(parents=True)
    (runtime / "python3").symlink_to("/usr/bin/python3")
    isolated_root = tmp_path / "isolated"
    isolated_root.mkdir()

    destination = _clone_models_for_lifecycle(
        source,
        isolated_root / "product-data/models/managed",
        isolation_root=isolated_root,
    )

    assert (destination / "model-packs/kokoro/records/smoke.json").is_file()
    assert not (destination / "runtimes").exists()


def test_macos_sandbox_proof_requires_control_denial_and_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = {
        "schema_version": 1,
        "probes": [{"host": "1.1.1.1", "port": 443, "connect_errno": 0}],
        "reachable_endpoints": [{"host": "1.1.1.1", "port": 443}],
    }
    monkeypatch.setattr("scripts.offline_bundle_acceptance.platform.system", lambda: "Darwin")
    monkeypatch.setenv(MACOS_SANDBOX_ACTIVE, "1")
    monkeypatch.setenv(MACOS_SANDBOX_CONTROL, json.dumps(control))
    monkeypatch.setattr(
        "scripts.offline_bundle_acceptance._tcp_probe",
        lambda _host, _port: {
            "host": "1.1.1.1",
            "port": 443,
            "connect_errno": 1,
            "errno_name": "EPERM",
            "duration_ms": 0.1,
        },
    )
    monkeypatch.setattr(
        "scripts.offline_bundle_acceptance._loopback_round_trip_probe",
        lambda: {"host": "127.0.0.1", "port": 43210, "round_trip_passed": True},
    )

    evidence = _verify_macos_sandbox_isolation()

    assert evidence["external_denial_errno_names"] == ["EPERM"]
    assert evidence["loopback_probe"]["round_trip_passed"] is True
    assert (
        evidence["sandbox_profile_sha256"]
        == hashlib.sha256(MACOS_SANDBOX_PROFILE.encode("utf-8")).hexdigest()
    )


def test_bundle_verification_does_not_ignore_nested_manifest_named_file(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    nested = root / "nested/bundle-manifest.json"
    nested.parent.mkdir()
    nested.write_text("undeclared", encoding="utf-8")

    with pytest.raises(ValueError, match="inventory mismatch"):
        verify_bundle(root)


@pytest.mark.parametrize("mutation", ["tamper", "extra", "symlink"])
def test_bundle_verification_fails_closed_for_tree_mutation(tmp_path: Path, mutation: str) -> None:
    root = _bundle(tmp_path)
    if mutation == "tamper":
        (root / "requirements.txt").write_text("changed", encoding="utf-8")
    elif mutation == "extra":
        (root / "surprise.txt").write_text("not declared", encoding="utf-8")
    else:
        (root / "install.py").unlink()
        (root / "install.py").symlink_to(root / "requirements.txt")

    with pytest.raises(ValueError):
        verify_bundle(root)


def test_bundle_verification_rejects_symlink_directory_and_manifest_link(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked-directory").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="link or reparse point"):
        verify_bundle(root)

    (root / "linked-directory").unlink()
    manifest = root / "bundle-manifest.json"
    external_manifest = tmp_path / "external-manifest.json"
    external_manifest.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(external_manifest)

    with pytest.raises(ValueError, match="link or reparse point"):
        verify_bundle(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [("size", True), ("sha256", "A" * 64), ("sha256", "not-a-hash")],
)
def test_bundle_verification_rejects_malformed_inventory_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    root = _bundle(tmp_path)
    manifest_path = root / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError):
        verify_bundle(root)


def test_bundle_verification_rejects_direct_or_index_requirement_sources(tmp_path: Path) -> None:
    for requirement in (
        "demo @ https://example.invalid/demo.whl --hash=sha256:" + "a" * 64,
        "--extra-index-url https://example.invalid/simple\ndemo==1 --hash=sha256:" + "a" * 64,
        "-r ../outside.txt",
    ):
        root = _bundle(tmp_path / hashlib.sha256(requirement.encode()).hexdigest()[:8])
        requirements = root / "requirements.txt"
        requirements.write_text(requirement + "\n", encoding="utf-8")
        manifest_path = root / "bundle-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(item for item in manifest["files"] if item["path"] == "requirements.txt")
        entry.update({"size": requirements.stat().st_size, "sha256": _sha256(requirements)})
        manifest["bundle_id"] = hashlib.sha256(
            json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ValueError, match=r"requirements\.txt"):
            verify_bundle(root)


def test_offline_install_environment_removes_proxies_and_disables_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/import/path")
    monkeypatch.setenv("VIRTUAL_ENV", "/untrusted/environment")

    environment = _offline_environment()

    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["PIP_CONFIG_FILE"]
    assert "HTTPS_PROXY" not in environment
    assert "http_proxy" not in environment
    assert "PYTHONPATH" not in environment
    assert "VIRTUAL_ENV" not in environment


def test_deterministic_zip_has_stable_bytes_and_timestamps(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    _deterministic_zip(root, first)
    _deterministic_zip(root, second)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


def test_deterministic_zip_refuses_symlinks_without_publishing_archive(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "unsafe-link").symlink_to(root / "requirements.txt")
    destination = tmp_path / "bundle.zip"

    with pytest.raises(ValueError, match="link or reparse point"):
        _deterministic_zip(root, destination)

    assert not destination.exists()


def test_installer_resumes_matching_interrupted_destination_without_deleting_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    manifest = verify_bundle(bundle)
    destination = tmp_path / "installed"
    destination.mkdir()
    (destination / "partial").write_text("old incomplete bytes", encoding="utf-8")
    (destination / ".whoopy-offline-installing.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "bundle_id": manifest["bundle_id"],
                "destination": str(destination.resolve()),
                "python_executable": str(Path(sys.executable).resolve()),
                "state": "installing",
                "attempt_id": "a" * 32,
            }
        ),
        encoding="utf-8",
    )

    def fake_run(_command: list[str], **_kwargs: object) -> object:
        class Completed:
            returncode = 0

        return Completed()

    monkeypatch.setattr("scripts.install_offline_bundle.subprocess.run", fake_run)

    receipt = install_bundle(bundle, destination)

    assert receipt["bundle_id"] == manifest["bundle_id"]
    assert (destination / "partial").read_text(encoding="utf-8") == "old incomplete bytes"
    assert not (destination / ".whoopy-offline-installing.json").exists()
    assert (destination / ".whoopy-offline-install.json").is_file()
    assert receipt["bundle_integrity_verified"] is True
    assert receipt["bundle_authenticity_verified"] is False
    assert receipt["package_index_isolation_enforced"] is True
    assert receipt["process_network_isolation_enforced"] is False


def test_installer_never_deletes_existing_destination_with_forged_legacy_marker(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    manifest = verify_bundle(bundle)
    destination = tmp_path / "installed"
    destination.mkdir()
    protected = destination / "preserve-me"
    protected.write_text("user data", encoding="utf-8")
    (destination / ".whoopy-offline-installing.json").write_text(
        json.dumps({"bundle_id": manifest["bundle_id"]}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="already exists"):
        install_bundle(bundle, destination)

    assert protected.read_text(encoding="utf-8") == "user data"


def test_failed_install_preserves_resumable_marker_and_never_deletes_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    destination = tmp_path / "installed"

    def fail_venv(_command: list[str], **_kwargs: object) -> object:
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr("scripts.install_offline_bundle.subprocess.run", fail_venv)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        install_bundle(bundle, destination)

    marker = json.loads(
        (destination / ".whoopy-offline-installing.json").read_text(encoding="utf-8")
    )
    assert marker["schema_version"] == 2
    assert marker["destination"] == str(destination.resolve())
    assert not (destination / ".whoopy-offline-install.json").exists()


def test_install_uses_private_verified_snapshot_and_hashes_product_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> object:
        commands.append(command)

        class Completed:
            returncode = 0

        return Completed()

    monkeypatch.setattr("scripts.install_offline_bundle.subprocess.run", fake_run)

    install_bundle(bundle, tmp_path / "installed")

    dependency_install = commands[1]
    product_install = commands[2]
    dependency_requirement = Path(dependency_install[dependency_install.index("--requirement") + 1])
    product_requirement = Path(product_install[product_install.index("--requirement") + 1])
    assert ".whoopy-input-" in str(dependency_requirement)
    assert bundle.resolve() not in dependency_requirement.parents
    assert {"--no-index", "--require-hashes"}.issubset(product_install)
    assert ".whoopy-input-" in str(product_requirement)


def test_installer_refuses_an_unowned_existing_destination(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "user-file").write_text("preserve me", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        install_bundle(bundle, destination)

    assert (destination / "user-file").read_text(encoding="utf-8") == "preserve me"


def test_builder_uses_lock_export_and_hash_required_binary_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / "scripts/install_offline_bundle.py").write_text("# installer\n", encoding="utf-8")
    (repository / "docs").mkdir()
    for guide in BUNDLE_GUIDES:
        (repository / "docs" / guide).write_text(f"# {guide}\n", encoding="utf-8")
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    wheel = repository / "dist/whoopy-9.8.7-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"whoopy")
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], *, cwd: Path, environment: dict[str, str] | None = None
    ) -> None:
        del cwd, environment
        commands.append(command)
        if "export" in command:
            Path(command[command.index("--output-file") + 1]).write_text(
                "demo==1 \\\n    --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
            )
        elif "venv" in command:
            environment_root = Path(command[-1])
            python = environment_root / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
        elif "download" in command:
            destination = Path(command[command.index("--destination-directory") + 1])
            (destination / "demo-1-py3-none-any.whl").write_bytes(b"dependency")

    monkeypatch.setattr("scripts.build_app_offline_bundle._run", fake_run)
    directory, archive = build_bundle(
        repository=repository,
        wheel=wheel,
        output_directory=tmp_path / "output",
        target=_target_id(),
        python=f"{sys.version_info.major}.{sys.version_info.minor}",
        uv="uv",
    )

    manifest = verify_bundle(directory)
    assert "9.8.7" in directory.name
    assert archive.is_file()
    assert archive.with_suffix(".zip.sha256").read_text(encoding="ascii") == (
        f"{_sha256(archive)}  {archive.name}\n"
    )
    assert manifest["lock_sha256"] == _sha256(repository / "uv.lock")
    assert {f"docs/{guide}" for guide in BUNDLE_GUIDES}.issubset(
        {str(item["path"]) for item in manifest["files"]}
    )
    export = commands[0]
    download = commands[2]
    assert {"--locked", "--no-dev", "--no-emit-project", "--no-header"}.issubset(export)
    assert {"--only-binary=:all:", "--require-hashes"}.issubset(download)
    assert any("--verify-only" in command for command in commands)


def test_acceptance_requires_a_network_isolation_description(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="network-isolation-note"):
        run_acceptance(
            bundle=tmp_path / "bundle",
            output=tmp_path / "report.json",
            network_disabled=True,
            network_isolation_note=None,
        )


def test_ci_style_bundle_proof_cannot_become_offline_or_platform_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "target": _target_id(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "bundle_id": "a" * 64,
        "product_version": "0.1.0",
        "product_wheel_sha256": "b" * 64,
        "lock_sha256": "c" * 64,
    }
    monkeypatch.setattr("scripts.offline_bundle_acceptance.verify_bundle", lambda _bundle: manifest)

    def fake_install(command: list[str], **_kwargs: object) -> object:
        prefix = Path(command[command.index("--prefix") + 1])
        prefix.mkdir(parents=True)
        (prefix / ".whoopy-offline-install.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "bundle_id": manifest["bundle_id"],
                    "bundle_target": manifest["target"],
                    "bundle_python": manifest["python"],
                    "product_version": manifest["product_version"],
                    "product_wheel_sha256": manifest["product_wheel_sha256"],
                    "bundle_integrity_verified": True,
                    "bundle_authenticity_verified": False,
                    "package_index_isolation_enforced": True,
                    "process_network_isolation_enforced": False,
                    "dependency_consistency_check_passed": True,
                    "launcher_smoke_passed": True,
                }
            ),
            encoding="utf-8",
        )

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("scripts.offline_bundle_acceptance.subprocess.run", fake_install)
    monkeypatch.setattr(
        "scripts.offline_bundle_acceptance._launch_and_probe",
        lambda *_args, **_kwargs: {
            "api_started": True,
            "bundled_frontend_served": True,
            "installed_defaults_resolved_without_checkout": True,
        },
    )
    monkeypatch.setattr(
        "scripts.offline_bundle_acceptance._probe_installed_interpreter",
        lambda *_args, **_kwargs: {
            "python": manifest["python"],
            "implementation": "CPython",
            "target": manifest["target"],
        },
    )

    report = run_acceptance(
        bundle=tmp_path / "bundle",
        output=tmp_path / "report.json",
        network_disabled=False,
        network_isolation_note=None,
    )

    assert report["packaging_passed"] is True
    assert report["offline_acceptance_passed"] is False
    assert report["package_index_isolation_enforced"] is True
    assert report["process_network_isolation_enforced"] is False
    assert report["independent_platform_evidence"] is False
    assert report["llm_prompt_generation_tested"] is False
    assert report["local_v1_release_ready"] is False

    attested = run_acceptance(
        bundle=tmp_path / "bundle",
        output=tmp_path / "attested-report.json",
        network_disabled=True,
        network_isolation_note="Operator disabled every host network adapter.",
    )
    assert attested["offline_launch_passed"] is True
    assert attested["offline_acceptance_passed"] is False
    assert attested["operator_attested_network_disabled_during_test"] is True
    assert attested["network_isolation_evidence_kind"] == "operator_attestation"
    assert attested["process_network_isolation_enforced"] is False
    assert attested["independent_platform_evidence"] is False
    assert attested["local_v1_release_ready"] is False

    monkeypatch.setattr(
        "scripts.offline_bundle_acceptance._launch_and_probe",
        lambda *_args, **_kwargs: {
            "api_started": True,
            "bundled_frontend_served": True,
            "installed_defaults_resolved_without_checkout": True,
            "studio_lifecycle_exercised": True,
            "studio_lifecycle_passed": True,
        },
    )
    managed_models = tmp_path / "models/managed"
    readiness = managed_models / "model-packs/kokoro/records/smoke.json"
    readiness.parent.mkdir(parents=True)
    readiness.write_text('{"state":"ready"}', encoding="utf-8")
    monkeypatch.setattr(
        "scripts.offline_bundle_acceptance._runtime_fingerprint_with_python",
        lambda *_args, **_kwargs: "a" * 64,
    )
    lifecycle = run_acceptance(
        bundle=tmp_path / "bundle",
        output=tmp_path / "lifecycle-report.json",
        network_disabled=True,
        network_isolation_note="Operator disabled every host network adapter.",
        models_directory=managed_models,
        lifecycle_output=tmp_path / "studio-lifecycle.json",
    )
    assert lifecycle["offline_acceptance_passed"] is True
    assert lifecycle["studio_lifecycle_passed"] is True
    assert lifecycle["voice_synthesis_tested"] is True
    assert lifecycle["llm_prompt_generation_tested"] is False
    assert lifecycle["independent_platform_evidence"] is False
    assert lifecycle["local_v1_release_ready"] is False
    assert lifecycle["isolated_models_directory_used"] is True
    assert lifecycle["runtime_fingerprint_match"] is True
    assert lifecycle["workspace_model_readiness_unchanged"] is True
