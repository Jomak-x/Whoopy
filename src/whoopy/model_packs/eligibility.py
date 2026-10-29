"""One fail-closed model eligibility contract for Studio, Lab, and release gates."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from whoopy.model_packs.registry import ModelPackState


class CheckLike(Protocol):
    check: str
    passed: bool
    message: str


class ModelEligibility(BaseModel):
    """Product-surface disposition derived only from current readiness evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ModelPackState
    studio_candidate: bool
    studio_selectable: bool
    lab_visible: bool = True
    machine_resolution: Literal["playable", "proven_incompatibility", "unresolved"]
    remedy: str | None


def _failed(checks: Sequence[CheckLike], *names: str) -> CheckLike | None:
    return next((item for item in checks if item.check in names and not item.passed), None)


def _passed(checks: Sequence[CheckLike], name: str) -> bool:
    return any(item.check == name and item.passed for item in checks)


def classify_model_eligibility(
    pack_id: str,
    state: ModelPackState | str,
    checks: Sequence[CheckLike],
    *,
    studio_candidate: bool = True,
    supported_platforms: Sequence[str] = (),
) -> ModelEligibility:
    """Derive selection and an exact next action without interpreting badges.

    ``studio_candidate`` is a reviewed catalog fact.  It never makes a model
    selectable: only current READY evidence does.  A resource/platform/runtime
    failure counts as a proven current-machine incompatibility only when the
    registry retained the failed check that explains it.
    """

    normalized = ModelPackState(state)
    readiness_evidence = all(
        _passed(checks, name)
        for name in ("runtime_probe", "offline_smoke_test", "playable_smoke_sample")
    )
    if normalized is ModelPackState.READY and readiness_evidence:
        return ModelEligibility(
            state=normalized,
            studio_candidate=studio_candidate,
            studio_selectable=studio_candidate,
            machine_resolution="playable",
            remedy=None,
        )
    if normalized is ModelPackState.READY:
        return ModelEligibility(
            state=normalized,
            studio_candidate=studio_candidate,
            studio_selectable=False,
            machine_resolution="unresolved",
            remedy=(
                "The ready status lacks complete current runtime, synthesis, and playable-sample "
                f"evidence for {pack_id}. Verify the pack and run Test voice again."
            ),
        )

    platform = _failed(checks, "platform")
    hardware = _failed(checks, "hardware_preflight")
    runtime = _failed(checks, "runtime_probe")
    smoke = _failed(checks, "offline_smoke_test", "playable_smoke_sample")
    deterministic = platform or hardware or runtime or smoke
    proven_incompatibility = (
        normalized
        in {
            ModelPackState.RESOURCE_BLOCKED,
            ModelPackState.INCOMPATIBLE,
        }
        and deterministic is not None
    )

    if platform is not None:
        supported = ", ".join(supported_platforms) or "a declared supported platform"
        remedy = f"{platform.message}. Use {pack_id} on {supported}, or choose Kokoro here."
    elif hardware is not None:
        remedy = (
            f"{hardware.message}. Free the stated live resources or use a machine meeting "
            "those requirements; choose Kokoro meanwhile."
        )
    elif normalized is ModelPackState.MISSING:
        remedy = f"Install {pack_id}, verify its pinned files, then run Test voice."
    elif normalized is ModelPackState.PARTIAL:
        remedy = f"Resume installation of {pack_id}, verify it, then run Test voice."
    elif normalized is ModelPackState.CORRUPT:
        remedy = f"Repair or reinstall {pack_id}, verify every digest, then run Test voice."
    elif _failed(checks, "isolated_runtime_files") is not None:
        remedy = (
            f"Install the checksum-reviewed isolated runtime declared for {pack_id}, "
            "then run Test voice. Copied development environments do not qualify."
        )
    elif runtime is not None:
        remedy = f"{runtime.message}. Repair the declared isolated runtime, then run Test voice."
    elif smoke is not None:
        remedy = f"{smoke.message}. Run Test voice again after repairing synthesis."
    else:
        remedy = f"Verify {pack_id} and run Test voice on this machine."

    return ModelEligibility(
        state=normalized,
        studio_candidate=studio_candidate,
        studio_selectable=False,
        machine_resolution="proven_incompatibility" if proven_incompatibility else "unresolved",
        remedy=remedy,
    )
