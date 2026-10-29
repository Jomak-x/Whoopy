from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from scripts.local_v1_acceptance_status import _parser
from whoopy.evaluation import PromotionDecision
from whoopy.model_packs import ModelPackState, ModelPackStatus, ReadinessCheck
from whoopy.release import load_acceptance_suite
from whoopy.release.status import (
    RELEASE_TARGETS,
    AcceptanceGateStatus,
    LocalV1AcceptanceStatus,
    _model_selection_role_evidence,
    assess_ci_portability,
    assess_corpus,
    assess_installed_bundles,
    assess_lab,
    assess_models,
    assess_package,
    assess_platforms,
    assess_studio_lifecycle,
)


def _installed_bundle_receipt(target: str) -> dict[str, object]:
    lifecycle = {
        "schema_version": 1,
        "evidence_scope": "localhost_studio_lifecycle_not_cross_platform_release",
        "status": "passed",
        "local_v1_release_ready": False,
        "steps": [
            {"step": step, "passed": True}
            for step in ("health", "generate", "playback", "edit", "export", "resume", "delete")
        ],
    }
    lifecycle_identity = hashlib.sha256(
        json.dumps(lifecycle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    bundle_id = hashlib.sha256(f"bundle:{target}".encode()).hexdigest()
    wheel_sha256 = hashlib.sha256(b"current portable product wheel").hexdigest()
    lock_sha256 = hashlib.sha256(f"lock:{target}".encode()).hexdigest()
    install_receipt = {
        "schema_version": 2,
        "bundle_id": bundle_id,
        "bundle_target": target,
        "bundle_python": "3.12",
        "product_version": "1.2.3",
        "product_wheel_sha256": wheel_sha256,
        "bundle_integrity_verified": True,
        "bundle_authenticity_verified": False,
        "package_index_isolation_enforced": True,
        "process_network_isolation_enforced": False,
        "dependency_consistency_check_passed": True,
        "launcher_smoke_passed": True,
    }
    install_identity = hashlib.sha256(
        (json.dumps(install_receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "evidence_scope": "fresh_application_bundle_offline_after_acquisition",
        "target": target,
        "product_version": "1.2.3",
        "python": "3.12",
        "bundle_id": bundle_id,
        "product_wheel_sha256": wheel_sha256,
        "dependency_lock_sha256": lock_sha256,
        "packaging_passed": True,
        "offline_launch_passed": True,
        "offline_acceptance_passed": True,
        "operator_attested_network_disabled_during_test": True,
        "process_network_isolation_enforced": False,
        "network_isolation_evidence_kind": "operator_attestation",
        "network_isolation_note": "Host network adapters were disabled for the complete test.",
        "studio_lifecycle_requested": True,
        "studio_lifecycle_exercised": True,
        "studio_lifecycle_passed": True,
        "voice_synthesis_tested": True,
        "independent_platform_evidence": False,
        "local_v1_release_ready": False,
        "api_started": True,
        "bundled_frontend_served": True,
        "installed_defaults_resolved_without_checkout": True,
        "bound_host": "127.0.0.1",
        "studio_lifecycle_evidence_sha256": lifecycle_identity,
        "studio_lifecycle_evidence": lifecycle,
        "install_receipt_sha256": install_identity,
        "install_receipt": install_receipt,
        "installed_interpreter": {
            "python": "3.12",
            "implementation": "CPython",
            "target": target,
        },
    }


def _write_installed_bundle_receipts(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for target in RELEASE_TARGETS:
        path = tmp_path / f"{target}.json"
        path.write_text(json.dumps(_installed_bundle_receipt(target)), encoding="utf-8")
        paths.append(path)
    return paths


def test_status_defaults_select_canonical_evidence_not_superseded_reports() -> None:
    args = _parser().parse_args([])

    assert args.corpus_report == Path("evaluations/local/corpus-acceptance-current.json")
    assert args.soak_report == Path("acceptance-reports/soak-exports-2026-08-05.json")
    assert args.interruption_report == Path(
        "acceptance-reports/forced-interruption-local-2026-08-05.json"
    )
    assert args.studio_lifecycle_report == Path("evaluations/local/studio-lifecycle-current.json")
    assert args.output == Path("acceptance-reports/local-v1-status-current.json")
    assert args.scenario == Path("config/acceptance/local-v1-platform-v1.json")
    assert args.installed_bundle_report == []

    selected_inputs = {
        args.corpus_report,
        args.soak_report,
        args.interruption_report,
        args.studio_lifecycle_report,
    }
    assert Path("acceptance-reports/local-v1-current.json") not in selected_inputs
    assert Path("acceptance-reports/local-v1-soak.json") not in selected_inputs


def test_studio_lifecycle_gate_requires_browser_resume_evidence(tmp_path: Path) -> None:
    report = tmp_path / "studio-lifecycle.json"
    report.write_text(
        json.dumps(
            {
                "evidence_scope": "localhost_studio_lifecycle_not_cross_platform_release",
                "status": "passed",
                "steps": [
                    {"step": step, "passed": True}
                    for step in ("health", "generate", "playback", "edit", "export", "delete")
                ],
            }
        ),
        encoding="utf-8",
    )

    gate = assess_studio_lifecycle(report)

    assert gate.outcome == "failed"
    assert gate.blockers == ("Missing passing Studio step: resume",)


def test_release_ready_cannot_be_set_when_a_required_gate_failed() -> None:
    failed = AcceptanceGateStatus(
        gate_id="corpus",
        title="Corpus",
        outcome="failed",
        detail="Human ratings are absent.",
    )
    with pytest.raises(ValidationError, match="must be derived"):
        LocalV1AcceptanceStatus(
            generated_at=datetime.now(UTC),
            product_version="0.1.0",
            declared_ci_targets=RELEASE_TARGETS,
            independently_observed_platforms={target: "passed" for target in RELEASE_TARGETS},
            model_readiness=(),
            gates=(failed,),
            blockers=(),
            local_v1_release_ready=True,
        )


def test_corpus_refuses_a_claim_without_all_ten_thresholds(tmp_path: Path) -> None:
    report = tmp_path / "corpus.json"
    report.write_text(
        json.dumps(
            {
                "accepted": True,
                "expected_case_count": 10,
                "covered_case_count": 9,
                "no_safety_failures": True,
                "complete_human_coverage": True,
                "complete_baseline_content_comparison": True,
                "content_improves_over_baseline": True,
                "content_median": 5,
                "voice_median": 5,
                "lowest_required_human_score": 5,
                "blockers": [],
            }
        ),
        "utf-8",
    )

    gate = assess_corpus(report)

    assert gate.outcome == "failed"
    assert gate.facts["covered_cases"] == 9


def test_ci_portability_is_never_a_required_platform_acceptance_gate(tmp_path: Path) -> None:
    targets = {
        target: {
            "evidence_scope": "ci_portability_not_release_acceptance",
            "passed": True,
            "os_network_isolation_proven": False,
        }
        for target in RELEASE_TARGETS
    }
    summary = tmp_path / "ci.json"
    summary.write_text(
        json.dumps(
            {
                "evidence_scope": "ci_portability_summary_not_release_acceptance",
                "passed": True,
                "targets": targets,
            }
        ),
        "utf-8",
    )
    suite = load_acceptance_suite(Path("config/acceptance/local-v1-primary-v1.json"))

    ci_gate = assess_ci_portability(summary)
    platform_gate, offline_gate, observed = assess_platforms(suite=suite)

    assert ci_gate.outcome == "passed"
    assert not ci_gate.required
    assert platform_gate.outcome == "missing"
    assert offline_gate.outcome == "missing"
    assert set(observed.values()) == {"not_run"}


def test_installed_bundle_gate_requires_a_bound_lifecycle_on_every_target(
    tmp_path: Path,
) -> None:
    paths = _write_installed_bundle_receipts(tmp_path)

    gate = assess_installed_bundles(tuple(paths), product_version="1.2.3")

    assert gate.outcome == "passed"
    assert gate.facts["passed_targets"] == sorted(RELEASE_TARGETS)


def test_installed_bundle_gate_accepts_bound_macos_process_sandbox_proof(
    tmp_path: Path,
) -> None:
    paths = _write_installed_bundle_receipts(tmp_path)
    path = paths[RELEASE_TARGETS.index("darwin-arm64")]
    receipt = json.loads(path.read_text("utf-8"))
    receipt.update(
        {
            "operator_attested_network_disabled_during_test": False,
            "process_network_isolation_enforced": True,
            "network_isolation_evidence_kind": "macos_sandbox_exec",
            "network_isolation_note": "Verified process sandbox with loopback exception.",
            "network_isolation_evidence": {
                "schema_version": 1,
                "control_kind": "pre_sandbox_reachability_then_in_sandbox_denial",
                "sandbox_executable": "/usr/bin/sandbox-exec",
                "sandbox_profile_sha256": hashlib.sha256(
                    b"(version 1)\n(allow default)\n(deny network-outbound)\n"
                    b'(allow network-outbound (remote ip "localhost:*"))\n'
                ).hexdigest(),
                "external_control": {
                    "schema_version": 1,
                    "probes": [{"host": "1.1.1.1", "port": 443, "connect_errno": 0}],
                    "reachable_endpoints": [{"host": "1.1.1.1", "port": 443}],
                },
                "external_denial_probes": [
                    {
                        "host": "1.1.1.1",
                        "port": 443,
                        "connect_errno": 1,
                        "errno_name": "EPERM",
                    }
                ],
                "loopback_probe": {
                    "host": "127.0.0.1",
                    "port": 43123,
                    "round_trip_passed": True,
                },
            },
        }
    )
    path.write_text(json.dumps(receipt), encoding="utf-8")

    gate = assess_installed_bundles(tuple(paths), product_version="1.2.3")

    assert gate.outcome == "passed"


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("install_receipt_sha256", "0" * 64),
        ("bundled_frontend_served", False),
        ("installed_defaults_resolved_without_checkout", False),
        ("bound_host", "0.0.0.0"),
    ),
)
def test_installed_bundle_gate_rejects_tampered_or_missing_probe_evidence(
    tmp_path: Path, field: str, replacement: object
) -> None:
    paths = _write_installed_bundle_receipts(tmp_path)
    receipt = json.loads(paths[0].read_text("utf-8"))
    receipt[field] = replacement
    paths[0].write_text(json.dumps(receipt), encoding="utf-8")

    gate = assess_installed_bundles(tuple(paths), product_version="1.2.3")

    assert gate.outcome == "failed"
    passed_targets = gate.facts["passed_targets"]
    assert isinstance(passed_targets, list)
    assert RELEASE_TARGETS[0] not in passed_targets


def test_installed_bundle_gate_rejects_interpreter_target_mismatch(tmp_path: Path) -> None:
    paths = _write_installed_bundle_receipts(tmp_path)
    receipt = json.loads(paths[0].read_text("utf-8"))
    receipt["installed_interpreter"]["target"] = "linux-arm64"
    paths[0].write_text(json.dumps(receipt), encoding="utf-8")

    gate = assess_installed_bundles(tuple(paths), product_version="1.2.3")

    assert gate.outcome == "failed"
    passed_targets = gate.facts["passed_targets"]
    assert isinstance(passed_targets, list)
    assert RELEASE_TARGETS[0] not in passed_targets


def test_installed_bundle_gate_binds_installer_python_to_probed_interpreter(
    tmp_path: Path,
) -> None:
    paths = _write_installed_bundle_receipts(tmp_path)
    receipt = json.loads(paths[0].read_text("utf-8"))
    receipt["install_receipt"]["bundle_python"] = "3.11"
    receipt["install_receipt_sha256"] = hashlib.sha256(
        (json.dumps(receipt["install_receipt"], indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    paths[0].write_text(json.dumps(receipt), encoding="utf-8")

    gate = assess_installed_bundles(tuple(paths), product_version="1.2.3")

    assert gate.outcome == "failed"
    passed_targets = gate.facts["passed_targets"]
    assert isinstance(passed_targets, list)
    assert RELEASE_TARGETS[0] not in passed_targets


def test_installed_bundle_gate_rejects_a_different_product_wheel_between_targets(
    tmp_path: Path,
) -> None:
    paths = _write_installed_bundle_receipts(tmp_path)
    receipt = json.loads(paths[0].read_text("utf-8"))
    other_wheel = hashlib.sha256(b"different same-version product wheel").hexdigest()
    receipt["product_wheel_sha256"] = other_wheel
    receipt["install_receipt"]["product_wheel_sha256"] = other_wheel
    receipt["install_receipt_sha256"] = hashlib.sha256(
        (json.dumps(receipt["install_receipt"], indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    paths[0].write_text(json.dumps(receipt), encoding="utf-8")

    gate = assess_installed_bundles(tuple(paths), product_version="1.2.3")

    assert gate.outcome == "failed"
    assert "product wheel" in " ".join(gate.blockers).lower()


def test_installed_bundle_gate_binds_receipts_to_current_packaged_wheel(tmp_path: Path) -> None:
    paths = _write_installed_bundle_receipts(tmp_path)

    gate = assess_installed_bundles(
        tuple(paths),
        product_version="1.2.3",
        expected_product_wheel_sha256=hashlib.sha256(b"rebuilt wheel").hexdigest(),
    )

    assert gate.outcome == "failed"
    assert "current packaged wheel digest" in " ".join(gate.blockers)


def _write_package(project: Path, *, extra_module: bool = False) -> tuple[Path, Path]:
    source = project / "src/whoopy/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text('__version__ = "1.2.3"\n', "utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "whoopy"\nversion = "1.2.3"\n', "utf-8"
    )
    (project / "uv.lock").write_text("version = 1\n", "utf-8")
    if extra_module:
        (source.parent / "retired.py").write_text("VALUE = 1\n", "utf-8")
    dist = project / "dist"
    dist.mkdir()
    wheel = dist / "whoopy-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("whoopy/__init__.py", source.read_bytes())
        if extra_module:
            archive.writestr("whoopy/retired.py", (source.parent / "retired.py").read_bytes())
        archive.writestr("whoopy-1.2.3.dist-info/METADATA", "Name: whoopy\nVersion: 1.2.3\n")
        archive.writestr("whoopy-1.2.3.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr("whoopy-1.2.3.dist-info/RECORD", "")
    sdist = dist / "whoopy-1.2.3.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for member_name, payload in (
            ("whoopy-1.2.3/pyproject.toml", (project / "pyproject.toml").read_bytes()),
            ("whoopy-1.2.3/src/whoopy/__init__.py", source.read_bytes()),
        ):
            info = tarfile.TarInfo(member_name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if extra_module:
            archive.add(source.parent / "retired.py", arcname="whoopy-1.2.3/src/whoopy/retired.py")
        for name in ("PKG-INFO", "setup.cfg", "src/whoopy.egg-info/SOURCES.txt"):
            info = tarfile.TarInfo(f"whoopy-1.2.3/{name}")
            archive.addfile(info, io.BytesIO())
    return source, dist


def test_package_freshness_compares_artifacts_to_actual_source(tmp_path: Path) -> None:
    source, dist = _write_package(tmp_path)

    assert assess_package(tmp_path, dist).outcome == "passed"
    source.write_text('__version__ = "changed"\n', "utf-8")

    stale = assess_package(tmp_path, dist)
    assert stale.outcome == "stale"
    assert any("Wheel does not match current source" in blocker for blocker in stale.blockers)


def test_package_freshness_rejects_deleted_modules_in_both_archives(tmp_path: Path) -> None:
    source, dist = _write_package(tmp_path, extra_module=True)

    assert assess_package(tmp_path, dist).outcome == "passed"
    (source.parent / "retired.py").unlink()

    stale = assess_package(tmp_path, dist)
    assert stale.outcome == "stale"
    assert any("Wheel source membership" in blocker for blocker in stale.blockers)
    assert any(
        "Source distribution contains absent or changed source" in blocker
        and "retired.py" in blocker
        for blocker in stale.blockers
    )


def test_package_freshness_covers_installed_config_and_worker_data(tmp_path: Path) -> None:
    source, dist = _write_package(tmp_path)
    config = tmp_path / "config/models.yaml"
    worker = tmp_path / "scripts/moss_tts_worker.py"
    config.parent.mkdir()
    worker.parent.mkdir()
    config.write_text("models: {}\n", "utf-8")
    worker.write_text("# pinned worker\n", "utf-8")

    wheel = dist / "whoopy-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("whoopy/__init__.py", source.read_bytes())
        archive.writestr(
            "whoopy-1.2.3.data/data/share/whoopy/config/models.yaml", config.read_bytes()
        )
        archive.writestr(
            "whoopy-1.2.3.data/data/share/whoopy/scripts/moss_tts_worker.py",
            worker.read_bytes(),
        )
        archive.writestr("whoopy-1.2.3.dist-info/METADATA", "Name: whoopy\nVersion: 1.2.3\n")
    sdist = dist / "whoopy-1.2.3.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for member_name, payload in (
            ("whoopy-1.2.3/pyproject.toml", (tmp_path / "pyproject.toml").read_bytes()),
            ("whoopy-1.2.3/src/whoopy/__init__.py", source.read_bytes()),
            ("whoopy-1.2.3/config/models.yaml", config.read_bytes()),
            ("whoopy-1.2.3/scripts/moss_tts_worker.py", worker.read_bytes()),
        ):
            info = tarfile.TarInfo(member_name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    assert assess_package(tmp_path, dist).outcome == "passed"
    config.write_text("models: {changed: true}\n", "utf-8")

    stale = assess_package(tmp_path, dist)
    assert stale.outcome == "stale"
    assert any("config/models.yaml" in blocker for blocker in stale.blockers)


def test_empty_lab_keeps_comparisons_and_human_decisions_missing(tmp_path: Path) -> None:
    comparisons, decisions = assess_lab(tmp_path)

    assert comparisons.outcome == "missing"
    assert decisions.outcome == "missing"
    assert "No durable blind Lab ratings exist." in decisions.blockers


def _model_decision(
    candidate_id: str,
    *,
    role: str | None,
    schema_version: int = 2,
    selected_value: str | None = None,
) -> PromotionDecision:
    return PromotionDecision.model_validate(
        {
            "schema_version": schema_version,
            "decision_id": str(uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            "experiment_id": "llm-review",
            "experiment_version": 1,
            "outcome": "promote",
            "studio_target": "llm",
            "selection_role": role,
            "candidate_id": candidate_id,
            "previous_value": "baseline",
            "selected_value": selected_value or candidate_id,
            "rationale": "Blind evidence supports this explicit model role.",
            "report_sha256": "a" * 64,
            "rating_ids": [str(uuid4())],
        }
    )


def test_model_selection_roles_require_distinct_portable_and_fallback_candidates() -> None:
    legacy = _model_decision("standard", role=None, schema_version=1)
    blockers, roles = _model_selection_role_evidence([legacy])

    assert roles == {}
    assert blockers == [
        "No evidence-backed model decision for required role: low_resource_fallback",
        "No evidence-backed model decision for required role: portable_default",
    ]

    same_candidate = [
        _model_decision("standard", role="portable_default"),
        _model_decision("standard", role="low_resource_fallback"),
    ]
    blockers, _roles = _model_selection_role_evidence(same_candidate)
    assert blockers == [
        "Portable default and low-resource fallback must select different model candidates."
    ]


def test_higher_quality_model_role_is_supported_but_not_required() -> None:
    required_only = [
        _model_decision("standard", role="portable_default"),
        _model_decision("lite", role="low_resource_fallback"),
    ]
    blockers, roles = _model_selection_role_evidence(required_only)

    assert blockers == []
    assert set(roles) == {"portable_default", "low_resource_fallback"}

    blockers, roles = _model_selection_role_evidence(
        [*required_only, _model_decision("studio", role="higher_quality")]
    )
    assert blockers == []
    assert roles["higher_quality"] == {"studio"}


def test_honest_distinct_standard_and_lite_roles_satisfy_status_gate() -> None:
    decisions = [
        _model_decision(
            "standard-qwen3-4b",
            role="portable_default",
            selected_value="qwen3-4b-standard",
        ),
        _model_decision(
            "lite-qwen3-1-7b",
            role="low_resource_fallback",
            selected_value="qwen3-1.7b-lite",
        ),
    ]

    blockers, roles = _model_selection_role_evidence(decisions)

    assert blockers == []
    assert roles == {
        "portable_default": {"standard-qwen3-4b"},
        "low_resource_fallback": {"lite-qwen3-1-7b"},
    }


class _EligibilityRegistry:
    def __init__(self, states: dict[str, tuple[ModelPackState, tuple[ReadinessCheck, ...]]]):
        self.states = states

    def list_statuses(self, *, verify_digests: bool) -> list[ModelPackStatus]:
        assert verify_digests
        return [
            ModelPackStatus(
                pack_id=pack_id,
                display_name=pack_id,
                revision="revision",
                state=state,
                selected_directory=Path(pack_id),
                license_id="Apache-2.0",
                license_url="https://www.apache.org/licenses/LICENSE-2.0",
                commercial_use_allowed=True,
                license_notice="test",
                files=[],
                checks=list(checks),
            )
            for pack_id, (state, checks) in self.states.items()
        ]

    def get(self, pack_id: str) -> SimpleNamespace:
        return SimpleNamespace(supported_platforms=["darwin-arm64"])


def test_model_gate_accepts_exact_incompatibility_but_not_missing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = (
        ReadinessCheck(check="runtime_probe", passed=True, message="runtime loaded"),
        ReadinessCheck(check="offline_smoke_test", passed=True, message="synthesized"),
        ReadinessCheck(check="playable_smoke_sample", passed=True, message="played"),
    )
    platform = (ReadinessCheck(check="platform", passed=False, message="target is not supported"),)
    resources = (
        ReadinessCheck(check="hardware_preflight", passed=False, message="requires 20 GB RAM"),
    )
    resolved = _EligibilityRegistry(
        {
            "kokoro": (ModelPackState.READY, ready),
            "fish-speech-1.4": (ModelPackState.INCOMPATIBLE, platform),
            "moss-local-5b": (ModelPackState.RESOURCE_BLOCKED, resources),
            "moss-8b": (ModelPackState.RESOURCE_BLOCKED, resources),
        }
    )
    monkeypatch.setattr(
        "whoopy.release.status.load_model_pack_registry", lambda *_a, **_k: resolved
    )

    gate, models = assess_models(tmp_path / "registry.yaml", tmp_path, verify_digests=True)

    assert gate.outcome == "passed"
    assert [item.pack_id for item in models if item.studio_selectable] == ["kokoro"]
    assert {item.pack_id for item in models if not item.compatible_on_current_machine} == {
        "fish-speech-1.4",
        "moss-local-5b",
        "moss-8b",
    }

    unresolved = _EligibilityRegistry(
        {
            **resolved.states,
            "fish-speech-1.4": (
                ModelPackState.INSTALLED,
                (
                    ReadinessCheck(
                        check="isolated_runtime_files", passed=False, message="runtime missing"
                    ),
                ),
            ),
        }
    )
    monkeypatch.setattr(
        "whoopy.release.status.load_model_pack_registry", lambda *_a, **_k: unresolved
    )

    gate, _ = assess_models(tmp_path / "registry.yaml", tmp_path, verify_digests=True)

    assert gate.outcome == "failed"
    assert any("checksum-reviewed isolated runtime" in blocker for blocker in gate.blockers)
