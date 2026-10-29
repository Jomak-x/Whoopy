from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from scripts.aggregate_ci_portability import EXPECTED_TARGETS, aggregate
from scripts.localhost_studio_lifecycle import (
    _wait_for_completion,
    _wait_for_interruption,
    _wait_for_resumable_script_progress,
    validated_base_url,
)


def _receipt(target: str, *, passed: bool = True) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_scope": "ci_portability_not_release_acceptance",
        "expected_target": target,
        "detected_target": target,
        "wheel_sha256": "a" * 64,
        "offline_process_environment": True,
        "os_network_isolation_proven": False,
        "passed": passed,
    }


def test_ci_portability_summary_cannot_claim_release_acceptance(tmp_path: Path) -> None:
    for target in EXPECTED_TARGETS:
        path = tmp_path / target / "receipt.json"
        path.parent.mkdir()
        path.write_text(json.dumps(_receipt(target)), encoding="utf-8")

    summary = aggregate(tmp_path)

    assert summary["passed"] is True
    assert summary["local_v1_release_ready"] is False
    assert summary["evidence_scope"] == "ci_portability_summary_not_release_acceptance"


def test_ci_portability_summary_reports_missing_and_failed_targets(tmp_path: Path) -> None:
    path = tmp_path / "windows.json"
    path.write_text(json.dumps(_receipt("windows-x86_64", passed=False)), encoding="utf-8")

    summary = aggregate(tmp_path)

    assert summary["passed"] is False
    missing_targets = cast(list[str], summary["missing_targets"])
    errors = cast(list[str], summary["errors"])
    assert "linux-arm64" in missing_targets
    assert any("windows-x86_64" in error for error in errors)


def test_studio_lifecycle_driver_refuses_non_loopback_urls() -> None:
    assert validated_base_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"

    for unsafe in (
        "https://127.0.0.1:8765",
        "http://example.test:8765",
        "http://localhost:8765/api",
        "http://user@localhost:8765",
    ):
        try:
            validated_base_url(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsafe lifecycle URL was accepted: {unsafe}")


def test_studio_lifecycle_waits_through_editor_revision_handoff() -> None:
    class Client:
        def __init__(self) -> None:
            states: tuple[dict[str, object], ...] = (
                {
                    "status": "interrupted",
                    "execution": {"interruption_kind": "editor_revision"},
                },
                {"status": "running", "execution": {"interruption_kind": None}},
                {"status": "completed", "execution": {"interruption_kind": None}},
            )
            self.states: Iterator[dict[str, object]] = iter(states)

        def json(self, method: str, path: str) -> dict[str, object]:
            assert method == "GET"
            assert path == "/api/v1/tasks/revision-run"
            return next(self.states)

    result = _wait_for_completion(
        Client(),  # type: ignore[arg-type]
        "revision-run",
        timeout_seconds=2,
        allow_editor_revision_resume=True,
    )

    assert result["status"] == "completed"


def test_studio_lifecycle_waits_for_a_durable_resumable_interruption() -> None:
    class Client:
        def __init__(self) -> None:
            states: tuple[dict[str, object], ...] = (
                {"status": "queued", "execution": {"interruption_kind": None}},
                {"status": "running", "execution": {"interruption_kind": None}},
                {
                    "status": "interrupted",
                    "execution": {"interruption_kind": "user_cancelled"},
                },
            )
            self.states: Iterator[dict[str, object]] = iter(states)

        def json(self, method: str, path: str) -> dict[str, object]:
            assert method == "GET"
            assert path == "/api/v1/tasks/resumable-run"
            return next(self.states)

    result = _wait_for_interruption(
        Client(),  # type: ignore[arg-type]
        "resumable-run",
        timeout_seconds=2,
    )

    assert result["status"] == "interrupted"


def test_studio_lifecycle_waits_through_resume_command_handoff() -> None:
    class Client:
        def __init__(self) -> None:
            states: tuple[dict[str, object], ...] = (
                {
                    "status": "interrupted",
                    "execution": {"interruption_kind": "user_cancelled"},
                },
                {"status": "running", "execution": {"interruption_kind": None}},
                {"status": "completed", "execution": {"interruption_kind": None}},
            )
            self.states: Iterator[dict[str, object]] = iter(states)

        def json(self, method: str, path: str) -> dict[str, object]:
            assert method == "GET"
            assert path == "/api/v1/tasks/resumed-run"
            return next(self.states)

    result = _wait_for_completion(
        Client(),  # type: ignore[arg-type]
        "resumed-run",
        timeout_seconds=2,
        allow_recovery_command_handoff=True,
    )

    assert result["status"] == "completed"


def test_studio_lifecycle_waits_for_immutable_script_before_cancelling() -> None:
    class Client:
        def __init__(self) -> None:
            states: tuple[dict[str, object], ...] = (
                {"status": "queued", "source_kind": "fixture_prompt"},
                {"status": "running", "source_kind": "fixture_prompt"},
                {"status": "running", "source_kind": "script_file"},
            )
            self.states: Iterator[dict[str, object]] = iter(states)

        def json(self, method: str, path: str) -> dict[str, object]:
            assert method == "GET"
            assert path == "/api/v1/tasks/script-run"
            return next(self.states)

    result = _wait_for_resumable_script_progress(
        Client(),  # type: ignore[arg-type]
        "script-run",
        timeout_seconds=2,
    )

    assert result["source_kind"] == "script_file"


def test_ci_third_party_actions_are_pinned_to_immutable_commits() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    for line in workflow.splitlines():
        stripped = line.strip()
        if not stripped.startswith("uses:"):
            continue
        reference = stripped.removeprefix("uses:").strip()
        if reference.startswith("./"):
            continue
        revision = reference.rsplit("@", 1)[-1]
        assert len(revision) == 40
        assert all(character in "0123456789abcdef" for character in revision)
