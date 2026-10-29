from __future__ import annotations

from whoopy.model_packs import ModelPackState, ReadinessCheck, classify_model_eligibility


def _check(name: str, passed: bool, message: str) -> ReadinessCheck:
    return ReadinessCheck(check=name, passed=passed, message=message)


def test_only_current_ready_evidence_makes_a_studio_candidate_selectable() -> None:
    ready = classify_model_eligibility(
        "kokoro",
        ModelPackState.READY,
        (
            _check("runtime_probe", True, "runtime loaded"),
            _check("offline_smoke_test", True, "real synthesis completed"),
            _check("playable_smoke_sample", True, "exact sample retained"),
        ),
    )
    installed = classify_model_eligibility(
        "fish-speech-1.4",
        ModelPackState.INSTALLED,
        (_check("isolated_runtime_files", False, "runtime missing"),),
    )

    assert ready.studio_selectable
    assert ready.machine_resolution == "playable"
    assert not installed.studio_selectable
    assert installed.machine_resolution == "unresolved"
    assert "checksum-reviewed isolated runtime" in (installed.remedy or "")


def test_exact_resource_or_platform_failure_is_a_proven_machine_disposition() -> None:
    resource = classify_model_eligibility(
        "moss-8b",
        ModelPackState.RESOURCE_BLOCKED,
        (_check("hardware_preflight", False, "requires 20 GB available RAM"),),
    )
    platform = classify_model_eligibility(
        "fish-speech-1.4",
        ModelPackState.INCOMPATIBLE,
        (_check("platform", False, "windows-x86_64 is not supported"),),
        supported_platforms=("darwin-arm64",),
    )

    assert resource.machine_resolution == "proven_incompatibility"
    assert "20 GB available RAM" in (resource.remedy or "")
    assert not resource.studio_selectable
    assert platform.machine_resolution == "proven_incompatibility"
    assert "darwin-arm64" in (platform.remedy or "")


def test_incompatibility_badge_without_failed_current_evidence_is_not_proof() -> None:
    eligibility = classify_model_eligibility("moss-local-5b", ModelPackState.INCOMPATIBLE, ())

    assert eligibility.machine_resolution == "unresolved"
    assert not eligibility.studio_selectable
    assert eligibility.remedy is not None


def test_lab_only_catalog_entry_stays_out_of_studio_even_if_ready() -> None:
    eligibility = classify_model_eligibility(
        "future-experimental",
        ModelPackState.READY,
        (
            _check("runtime_probe", True, "runtime loaded"),
            _check("offline_smoke_test", True, "real synthesis completed"),
            _check("playable_smoke_sample", True, "exact sample retained"),
        ),
        studio_candidate=False,
    )

    assert eligibility.machine_resolution == "playable"
    assert not eligibility.studio_selectable
    assert eligibility.lab_visible


def test_ready_badge_without_complete_current_evidence_is_not_selectable() -> None:
    eligibility = classify_model_eligibility(
        "copied-status",
        ModelPackState.READY,
        (_check("playable_smoke_sample", True, "sample record copied"),),
    )

    assert not eligibility.studio_selectable
    assert eligibility.machine_resolution == "unresolved"
    assert "lacks complete current" in (eligibility.remedy or "")
