from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import psutil
import pytest

from scripts.acceptance_production_case import _rounded_duration_ms
from scripts.forced_interruption_case import _plan_semantic_digest
from scripts.local_v1_acceptance import main as acceptance_main
from whoopy import __version__
from whoopy.hardware import HardwareSnapshot
from whoopy.pipeline import RunStore
from whoopy.pipeline.acceptance_stage import AcceptanceStageMarker
from whoopy.release import (
    AcceptanceCase,
    AcceptanceCaseResult,
    AcceptanceReport,
    MigrationReport,
    OfflineAuditReport,
    acceptance_suite_sha256,
    aggregate_platform_reports,
    audit_offline_post_acquisition,
    build_platform_matrix,
    detect_release_target,
    inspect_migrations,
    inspect_release_hardware,
    load_acceptance_suite,
    run_acceptance,
    select_friendly_backend,
    write_acceptance_report,
    write_cross_platform_evidence,
)
from whoopy.release.acceptance import (
    AuditCheck,
    ProcessEvidence,
    ProductionArtifactEvidence,
    ProductionCaseReceipt,
    _attach_production_receipt,
    _offline_environment,
    _resolve_command,
)
from whoopy.release.status import assess_platforms
from whoopy.timeline import build_prompt_timeline


def _hardware(*accelerators: str) -> HardwareSnapshot:
    return HardwareSnapshot(
        operating_system="linux",
        architecture="x86_64",
        cpu_count=8,
        total_ram_gb=32,
        available_ram_gb=20,
        free_disk_gb=100,
        accelerators=["cpu", *accelerators],
    )


def test_production_receipt_rounds_frame_derived_fractional_milliseconds() -> None:
    assert _rounded_duration_ms(60_000.25) == 60_000
    assert _rounded_duration_ms(60_000.75) == 60_001


def test_drafting_plan_digest_ignores_only_derived_timing_fields(tmp_path: Path) -> None:
    initial = {
        "title": "Grounding",
        "silence_budget_seconds": 20,
        "sections": [
            {
                "id": "practice",
                "technique": "grounding",
                "purpose": "quiet practice",
                "pause_after_ms": 8_000,
            }
        ],
    }
    refined = json.loads(json.dumps(initial))
    refined["silence_budget_seconds"] = 43
    refined["sections"][0]["pause_after_ms"] = 31_000
    initial_path = tmp_path / "initial.json"
    refined_path = tmp_path / "refined.json"
    initial_path.write_text(json.dumps(initial), encoding="utf-8")
    refined_path.write_text(json.dumps(refined), encoding="utf-8")

    assert _plan_semantic_digest(initial_path) == _plan_semantic_digest(refined_path)

    refined["sections"][0]["technique"] = "visualization"
    refined_path.write_text(json.dumps(refined), encoding="utf-8")
    assert _plan_semantic_digest(initial_path) != _plan_semantic_digest(refined_path)


def _passing_offline() -> OfflineAuditReport:
    return OfflineAuditReport(
        passed=True,
        network_disabled_during_test=True,
        network_isolation_evidence_kind="operator_attestation",
        network_isolation_note="Test host network adapters were disabled.",
        required_pack_ids=("kokoro",),
        checks=(AuditCheck(check="fixture", passed=True, detail="verified"),),
    )


def _macos_sandbox_evidence() -> dict[str, object]:
    return {
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
            "port": 43210,
            "round_trip_passed": True,
        },
    }


def _passing_migrations() -> MigrationReport:
    return MigrationReport(
        findings=(),
        passed=True,
        current_count=0,
        legacy_readable_count=0,
        incompatible_count=0,
    )


def test_release_matrix_contains_only_five_declared_targets_and_no_inferred_passes() -> None:
    current = detect_release_target(operating_system="Darwin", architecture="aarch64")

    matrix = build_platform_matrix(
        current,
        current_status="passed",
        current_evidence="Local suite passed.",
    )

    assert current.target_id == "darwin-arm64"
    assert len(matrix) == 5
    assert {entry.target.target_id for entry in matrix} == {
        "windows-x86_64",
        "darwin-arm64",
        "darwin-x86_64",
        "linux-x86_64",
        "linux-arm64",
    }
    assert sum(entry.status == "passed" for entry in matrix) == 1
    assert all(entry.status == "not_run" for entry in matrix if not entry.current_machine)
    assert (
        detect_release_target(operating_system="windows", architecture="arm64").supported is False
    )


def test_friendly_backend_selection_has_deterministic_fallback_order() -> None:
    assert select_friendly_backend(_hardware("cuda", "vulkan")).backend == "cuda"
    assert select_friendly_backend(_hardware("vulkan")).backend == "vulkan"
    cpu = select_friendly_backend(_hardware())
    assert cpu.backend == "cpu"
    assert cpu.display_name == "Automatic - processor"

    mac = _hardware("metal").model_copy(
        update={"operating_system": "darwin", "architecture": "arm64"}
    )
    assert select_friendly_backend(mac).backend == "metal"
    assert select_friendly_backend(mac).display_name == "Automatic - Apple GPU"


def test_release_hardware_adds_vulkan_only_after_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("whoopy.release.platforms.inspect_hardware", lambda _path: _hardware())
    monkeypatch.setattr("whoopy.release.platforms._has_working_vulkan", lambda: True)

    snapshot = inspect_release_hardware(Path("."))

    assert "vulkan" in snapshot.accelerators
    assert select_friendly_backend(snapshot).backend == "vulkan"


def test_offline_audit_refuses_to_claim_unproven_network_or_models(tmp_path: Path) -> None:
    report = audit_offline_post_acquisition(
        config_directory=Path("config"),
        models_root=tmp_path / "models",
        required_pack_ids=(),
        network_disabled_during_test=False,
    )

    assert report.passed is False
    checks = {check.check: check for check in report.checks}
    assert checks["remote_fallback_disabled"].passed is True
    assert checks["os_network_isolation"].passed is False
    assert checks["required_model_packs"].passed is False


def test_offline_report_schema_rejects_pass_without_explicit_isolation_proof() -> None:
    with pytest.raises(ValueError, match="validated macOS sandbox proof"):
        OfflineAuditReport(
            passed=True,
            network_disabled_during_test=True,
            required_pack_ids=("kokoro",),
            checks=(AuditCheck(check="fixture", passed=True, detail="verified"),),
        )


def test_offline_report_accepts_validated_macos_process_isolation() -> None:
    report = OfflineAuditReport(
        passed=True,
        network_disabled_during_test=True,
        network_isolation_evidence_kind="macos_sandbox_exec",
        network_isolation_note="Verified macOS process sandbox.",
        process_network_isolation_enforced=True,
        network_isolation_evidence=_macos_sandbox_evidence(),
        required_pack_ids=("kokoro",),
        checks=(AuditCheck(check="fixture", passed=True, detail="verified"),),
    )

    assert report.passed is True


def test_migration_inspection_recognizes_readable_legacy_and_invalid_data(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    store = RunStore(runs)
    record = store.create("A migration test.")
    timeline = build_prompt_timeline(
        run_id=record.run_id,
        prompt=record.prompt,
        created_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
    )
    (store.run_directory(record.run_id) / "timeline.json").write_text(
        timeline.model_dump_json(indent=2), encoding="utf-8"
    )
    corrupt = runs / str(UUID("11111111-1111-4111-8111-111111111111"))
    corrupt.mkdir()
    (corrupt / "run.json").write_text('{"schema_version": 999}', encoding="utf-8")

    report = inspect_migrations(
        runs_directory=runs,
        config_directory=Path("config"),
        models_root=tmp_path / "models",
    )

    assert report.passed is False
    assert report.legacy_readable_count >= 2
    assert report.incompatible_count == 1
    invalid = next(item for item in report.findings if item.path == corrupt / "run.json")
    assert invalid.state == "incompatible"


def test_migration_inspection_validates_managed_runtime_receipts(tmp_path: Path) -> None:
    runtime = tmp_path / "models/managed/runtimes/example"
    runtime.mkdir(parents=True)
    receipt = runtime / ".whoopy-runtime.json"
    receipt.write_text(
        '{"schema_version":1,"runtime_id":"example","revision":"r1",'
        '"upstream_revision":"a1","target":"linux-x86_64",'
        '"bundle_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"installed_tree_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        '"installed_at":"2026-08-05T00:00:00Z"}',
        encoding="utf-8",
    )

    report = inspect_migrations(
        runs_directory=tmp_path / "runs",
        config_directory=Path("config"),
        models_root=tmp_path / "models",
    )

    finding = next(item for item in report.findings if item.path == receipt)
    assert finding.category == "runtime_record"
    assert finding.state == "current"


def test_current_schema_run_without_audience_is_reported_as_legacy_readable(
    tmp_path: Path,
) -> None:
    run_id = UUID("21111111-1111-4111-8111-111111111111")
    directory = tmp_path / "runs" / str(run_id)
    directory.mkdir(parents=True)
    path = directory / "run.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 6,
                "run_id": str(run_id),
                "status": "queued",
                "prompt": "A legacy queued prompt.",
                "source_kind": "generated_prompt",
                "created_at": "2026-08-05T00:00:00Z",
                "updated_at": "2026-08-05T00:00:00Z",
                "generation_request_artifact": "generation-request.json",
                "recovery": {
                    "process_attempts": 0,
                    "resume_count": 0,
                    "cache_hits": 0,
                    "cache_misses": 0,
                    "checkpoint_reuses": 0,
                    "speech_segments_total": 0,
                    "speech_segments_completed": 0,
                },
                "execution": {"stage": "queued"},
            }
        ),
        encoding="utf-8",
    )

    report = inspect_migrations(
        runs_directory=tmp_path / "runs",
        config_directory=Path("config"),
        models_root=tmp_path / "models",
    )

    finding = next(item for item in report.findings if item.path == path)
    assert finding.state == "legacy_readable"
    assert "lacks current field(s): audience" in finding.detail


def test_migration_inspection_checks_cache_records_and_rejects_future_schemas(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "runs/.cache/segments/aa" / ("a" * 64) / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "cache_key": "a" * 64,
                "synthesis_input_sha256": "a" * 64,
                "synthesizer_identity": "fixture",
                "sample_rate": 24000,
                "frame_count": 100,
                "byte_count": 200,
                "pcm_sha256": "b" * 64,
                "created_at": "2026-08-05T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    report = inspect_migrations(
        runs_directory=tmp_path / "runs",
        config_directory=Path("config"),
        models_root=tmp_path / "models",
    )

    finding = next(item for item in report.findings if item.path == metadata)
    assert finding.category == "cache"
    assert finding.state == "incompatible"
    assert report.passed is False


def test_soak_and_forced_interruption_generate_honest_local_report(tmp_path: Path) -> None:
    marker = tmp_path / "ready"
    recovered = tmp_path / "recovered"
    soak = AcceptanceCase(
        case_id="two-soak-runs",
        kind="soak",
        command=(sys.executable, "-c", "print('playable export')"),
        repetitions=2,
        timeout_seconds=5,
    )
    interruption = AcceptanceCase(
        case_id="interrupt-synthesis",
        kind="forced_interruption",
        interruption_stage="synthesis",
        command=(
            sys.executable,
            "-c",
            (
                "import datetime,json,os,pathlib,time; "
                "value={'schema_version':1,'run_id':'11111111-1111-4111-8111-111111111111',"
                "'stage':'synthesis','process_id':os.getpid(),"
                "'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),"
                "'durable_run_status':'running','durable_run_stage':'synthesizing'}; "
                f"pathlib.Path({str(marker)!r}).write_text(json.dumps(value)); time.sleep(30)"
            ),
        ),
        ready_file=marker,
        recovery_command=(
            sys.executable,
            "-c",
            f"import pathlib; pathlib.Path({str(recovered)!r}).write_text('resumed')",
        ),
        timeout_seconds=5,
    )

    report = run_acceptance(
        cases=(soak, interruption),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        hardware=_hardware(),
        generated_at=datetime(2026, 8, 5, 13, tzinfo=UTC),
    )

    assert report.current_machine_passed is True
    assert report.local_v1_release_ready is False
    assert all(result.passed for result in report.cases)
    assert report.cases[1].interruption_observed is True
    assert report.cases[1].stage_marker is not None
    assert report.cases[1].stage_marker.stage == "synthesis"
    assert recovered.read_text(encoding="utf-8") == "resumed"
    assert sum(entry.status == "passed" for entry in report.platform_matrix) == 1
    assert sum(entry.status == "not_run" for entry in report.platform_matrix) == 4

    output = tmp_path / "reports" / "acceptance.json"
    write_acceptance_report(output, report)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["target"]["target_id"] == "linux-x86_64"
    assert saved["local_v1_release_ready"] is False


def test_forced_interruption_accepts_stage_marker_from_child_interpreter(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "child.ready"
    recovered = tmp_path / "child.recovered"
    wrapper_pid_path = tmp_path / "wrapper.pid"
    child = tmp_path / "stage_child.py"
    child.write_text(
        "import datetime, json, os, pathlib, time\n"
        "value = {\n"
        "    'schema_version': 1,\n"
        "    'run_id': '11111111-1111-4111-8111-111111111111',\n"
        "    'stage': 'synthesis',\n"
        "    'process_id': os.getpid(),\n"
        "    'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),\n"
        "    'durable_run_status': 'running',\n"
        "    'durable_run_stage': 'synthesizing',\n"
        "}\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(value))\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    wrapper = (
        "import os, pathlib, subprocess, sys; "
        f"pathlib.Path({str(wrapper_pid_path)!r}).write_text(str(os.getpid())); "
        f"child = subprocess.Popen([sys.executable, {str(child)!r}]); "
        "sys.exit(child.wait())"
    )
    case = AcceptanceCase(
        case_id="child-interpreter-stage",
        kind="forced_interruption",
        interruption_stage="synthesis",
        command=(sys.executable, "-c", wrapper),
        ready_file=marker,
        recovery_command=(
            sys.executable,
            "-c",
            f"import pathlib; pathlib.Path({str(recovered)!r}).write_text('resumed')",
        ),
        timeout_seconds=5,
    )

    report = run_acceptance(
        cases=(case,),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        hardware=_hardware(),
    )

    result = report.cases[0]
    assert result.passed is True
    assert result.stage_marker is not None
    wrapper_pid = int(wrapper_pid_path.read_text(encoding="utf-8"))
    assert result.stage_marker.process_id != wrapper_pid
    assert psutil.pid_exists(result.stage_marker.process_id) is False
    assert psutil.pid_exists(wrapper_pid) is False
    assert recovered.read_text(encoding="utf-8") == "resumed"


def test_forced_interruption_rejects_stage_marker_from_unrelated_process(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "unrelated.ready"
    recovered = tmp_path / "unrelated.recovered"
    unrelated_pid = os.getpid()
    marker_script = (
        "import datetime, json, pathlib, time; "
        "value={"
        "'schema_version':1,"
        "'run_id':'11111111-1111-4111-8111-111111111111',"
        "'stage':'synthesis',"
        f"'process_id':{unrelated_pid},"
        "'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),"
        "'durable_run_status':'running',"
        "'durable_run_stage':'synthesizing'"
        "}; "
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(value)); "
        "time.sleep(30)"
    )
    case = AcceptanceCase(
        case_id="unrelated-process-stage",
        kind="forced_interruption",
        interruption_stage="synthesis",
        command=(sys.executable, "-c", marker_script),
        ready_file=marker,
        recovery_command=(
            sys.executable,
            "-c",
            f"import pathlib; pathlib.Path({str(recovered)!r}).write_text('resumed')",
        ),
        timeout_seconds=5,
    )

    report = run_acceptance(
        cases=(case,),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        hardware=_hardware(),
    )

    result = report.cases[0]
    assert result.passed is False
    assert result.stage_marker is None
    assert result.recovery is None
    assert "did not belong to the acceptance process tree" in result.detail
    assert recovered.exists() is False


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX session and signal behavior")
@pytest.mark.parametrize("detached_child", [False, True])
def test_acceptance_timeout_reaps_generation_child_before_next_case(
    tmp_path: Path, detached_child: bool
) -> None:
    child_pid = tmp_path / "child.pid"
    heartbeat = tmp_path / "heartbeat"
    child_script = tmp_path / "generation.py"
    child_script.write_text(
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "while True:\n"
        f"    pathlib.Path({str(heartbeat)!r}).write_text(str(time.monotonic_ns()))\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    wrapper = (
        "import subprocess, sys; "
        "print('acceptance wrapper started', flush=True); "
        f"child = subprocess.Popen([sys.executable, {str(child_script)!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        f"start_new_session={detached_child!r}); "
        "child.wait(); print('generation child reaped', flush=True)"
    )
    next_case = (
        "import pathlib, psutil; "
        f"pid = int(pathlib.Path({str(child_pid)!r}).read_text()); "
        "assert not psutil.pid_exists(pid), 'generation child survived timeout'; "
        "print('next case has exclusive process ownership')"
    )
    try:
        report = run_acceptance(
            cases=(
                AcceptanceCase(
                    case_id="timed-out-generation",
                    kind="soak",
                    command=(sys.executable, "-c", wrapper),
                    timeout_seconds=1,
                ),
                AcceptanceCase(
                    case_id="following-generation",
                    kind="soak",
                    command=(sys.executable, "-c", next_case),
                    timeout_seconds=5,
                ),
            ),
            offline=_passing_offline(),
            migrations=_passing_migrations(),
            hardware=_hardware(),
        )
        assert child_pid.is_file()
        assert heartbeat.is_file()
        attempt = report.cases[0].attempts[0]
        assert attempt.return_code == 124
        assert "acceptance wrapper started" in attempt.stdout_tail
        assert "Acceptance command timed out" in attempt.stderr_tail
        assert report.cases[0].passed is False
        assert report.cases[1].passed is True
        assert not psutil.pid_exists(int(child_pid.read_text()))
    finally:
        if child_pid.exists():
            try:
                child = psutil.Process(int(child_pid.read_text()))
                child.kill()
                psutil.wait_procs([child], timeout=2)
            except psutil.NoSuchProcess:
                pass


@pytest.mark.parametrize("failure", ["timeout", "exception"])
@pytest.mark.parametrize(
    "detached_child",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(os.name != "posix", reason="Requires POSIX sessions"),
        ),
    ],
)
def test_forced_interruption_failure_reaps_children_and_retains_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, detached_child: bool
) -> None:
    child_pid = tmp_path / "child.pid"
    marker = tmp_path / "stage.ready"
    partial_artifact = tmp_path / "partial.txt"
    child_script = tmp_path / "generation.py"
    marker_write = (
        f"pathlib.Path({str(marker)!r}).write_text('ready')\n" if failure == "exception" else ""
    )
    child_script.write_text(
        "import os, pathlib, signal, sys, time\n"
        "if os.name == 'posix':\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        f"pathlib.Path({str(partial_artifact)!r}).write_text('partial generation')\n"
        "print('partial stdout', flush=True)\n"
        "print('partial stderr', file=sys.stderr, flush=True)\n" + marker_write + "while True:\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    wrapper = (
        "import subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, {str(child_script)!r}], "
        f"start_new_session={detached_child!r}); "
        "child.wait()"
    )
    next_case = (
        "import pathlib, psutil; "
        f"pid = int(pathlib.Path({str(child_pid)!r}).read_text()); "
        "assert not psutil.pid_exists(pid), 'generation child survived interruption'; "
        "print('next case has exclusive process ownership')"
    )
    if failure == "exception":

        def fail_marker(
            case: AcceptanceCase, *, expected_process_ids: tuple[int, ...]
        ) -> tuple[AcceptanceStageMarker | None, str | None]:
            raise RuntimeError("marker inspection failed")

        monkeypatch.setattr("whoopy.release.acceptance._load_stage_marker", fail_marker)
    try:
        report = run_acceptance(
            cases=(
                AcceptanceCase(
                    case_id="failed-interruption",
                    kind="forced_interruption",
                    interruption_stage="drafting",
                    command=(sys.executable, "-c", wrapper),
                    ready_file=marker,
                    recovery_command=(sys.executable, "-c", "raise AssertionError('no recovery')"),
                    timeout_seconds=1 if failure == "timeout" else 5,
                ),
                AcceptanceCase(
                    case_id="following-generation",
                    kind="soak",
                    command=(sys.executable, "-c", next_case),
                    timeout_seconds=5,
                ),
            ),
            offline=_passing_offline(),
            migrations=_passing_migrations(),
            hardware=_hardware(),
        )
        result = report.cases[0]
        assert result.passed is False
        assert result.interruption_observed is False
        assert result.recovery is None
        assert len(result.attempts) == 1
        assert "partial stdout" in result.attempts[0].stdout_tail
        assert "partial stderr" in result.attempts[0].stderr_tail
        assert partial_artifact.read_text() == "partial generation"
        if failure == "exception":
            assert "RuntimeError: marker inspection failed" in result.detail
        assert report.cases[1].passed is True
        assert not psutil.pid_exists(int(child_pid.read_text()))
    finally:
        if child_pid.exists():
            try:
                child = psutil.Process(int(child_pid.read_text()))
                child.kill()
                psutil.wait_procs([child], timeout=2)
            except psutil.NoSuchProcess:
                pass


def test_harness_records_missing_executable_as_failure_instead_of_losing_report() -> None:
    case = AcceptanceCase(
        case_id="missing-tool",
        kind="soak",
        command=("whoopy-command-that-does-not-exist",),
    )

    report = run_acceptance(
        cases=(case,),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        hardware=_hardware(),
    )

    assert report.current_machine_passed is False
    assert report.cases[0].status == "failed"
    assert "not found or is not runnable" in report.cases[0].detail


def test_shipped_acceptance_suite_declares_full_soak_and_interruption_matrix() -> None:
    suite = load_acceptance_suite(Path("config/acceptance/local-v1-primary-v1.json"))

    assert suite.suite_id == "local-v1-primary-v1"
    assert suite.suite_scope == "primary_machine_full"
    soaks = [case for case in suite.cases if case.kind == "soak"]
    assert sum(case.repetitions for case in soaks) == 20
    assert {case.duration_minutes for case in soaks} == {1, 3, 5, 10, 20}
    interruptions = [case for case in suite.cases if case.kind == "forced_interruption"]
    assert {case.interruption_stage for case in interruptions} == {
        "planning",
        "drafting",
        "synthesis",
        "assembly",
        "encoding",
        "qc",
    }
    assert all(case.disposition == "run" for case in interruptions)
    assert all(case.ready_file is not None for case in interruptions)
    assert all(
        any(part.endswith("forced_interruption_case.py") for part in case.command)
        for case in interruptions
    )
    assert all(case.command[0] == "{python}" for case in suite.cases)
    assert all(
        case.recovery_command is None or case.recovery_command[0] == "{python}"
        for case in suite.cases
    )


def test_acceptance_python_placeholder_resolves_to_current_cross_platform_interpreter() -> None:
    resolved = _resolve_command(("{python}", "-c", "print('ok')"))

    assert resolved[0] == str(Path(sys.executable).absolute())


def test_acceptance_environment_disables_acquisition_and_import_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setenv("VIRTUAL_ENV", "/untrusted-venv")

    environment = _offline_environment()

    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert "PYTHONPATH" not in environment
    assert "VIRTUAL_ENV" not in environment


def test_soak_receipt_is_embedded_and_hash_bound_to_the_platform_report(tmp_path: Path) -> None:
    artifact = ProductionArtifactEvidence(path=tmp_path / "audio.wav", sha256="a" * 64, bytes=4)
    receipt = ProductionCaseReceipt(
        schema_version=2,
        evidence_scope="production_prompt_to_audio_case",
        generated_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
        run_id=UUID("31111111-1111-4111-8111-111111111111"),
        command=("{python}", "scripts/acceptance_production_case.py"),
        profile="standard",
        duration_minutes=3,
        actual_duration_ms=180_000,
        quality_passed=True,
        artifacts={
            "run": artifact,
            "timeline": artifact,
            "audio": artifact,
            "manifest": artifact,
            "quality": artifact,
        },
    )
    path = tmp_path / "receipt.json"
    path.write_text(receipt.model_dump_json(), encoding="utf-8")
    process = ProcessEvidence(
        return_code=0,
        duration_seconds=1,
        stdout_tail=f"generation logs\n{path}\n",
        stderr_tail="",
    )

    embedded = _attach_production_receipt(process, expected_minutes=3)

    assert embedded.return_code == 0
    assert embedded.production_receipt == receipt
    assert embedded.receipt_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_pending_stage_is_explicitly_not_run_and_blocks_current_machine_pass() -> None:
    case = AcceptanceCase(
        case_id="pending-qc",
        kind="forced_interruption",
        disposition="pending",
        evidence_note="No deterministic production stage marker exists.",
        interruption_stage="qc",
        command=(sys.executable, "-c", "raise SystemExit('must not run')"),
        ready_file=Path("qc.ready"),
        recovery_command=(sys.executable, "-c", "raise SystemExit('must not run')"),
    )

    report = run_acceptance(
        cases=(case,),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        hardware=_hardware(),
    )

    assert report.current_machine_passed is False
    assert report.cases[0].status == "not_run"
    assert "No success is inferred" in report.cases[0].detail


def test_forced_interruption_removes_stale_marker_and_refuses_false_pass(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "stale.ready"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "11111111-1111-4111-8111-111111111111",
                "stage": "qc",
                "process_id": 1,
                "observed_at": "2026-08-05T12:00:00Z",
                "durable_run_status": "running",
                "durable_run_stage": "quality_check",
            }
        ),
        encoding="utf-8",
    )
    case = AcceptanceCase(
        case_id="stale-marker",
        kind="forced_interruption",
        interruption_stage="qc",
        command=(sys.executable, "-c", "import time; time.sleep(2)"),
        ready_file=marker,
        recovery_command=(sys.executable, "-c", "raise SystemExit(0)"),
        timeout_seconds=0.1,
    )

    report = run_acceptance(
        cases=(case,),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        hardware=_hardware(),
    )

    assert report.cases[0].passed is False
    assert report.cases[0].stage_marker is None
    assert report.cases[0].recovery is None
    assert marker.exists() is False


def test_external_declaration_cannot_create_a_current_machine_pass() -> None:
    case = AcceptanceCase(
        case_id="external-windows-soak",
        kind="soak",
        disposition="external",
        evidence_note="Must be executed on Windows x64.",
        target_id="windows-x86_64",
        duration_minutes=1,
        command=(sys.executable, "-c", "raise SystemExit('must not run')"),
    )

    report = run_acceptance(
        cases=(case,),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        hardware=_hardware(),
    )

    assert report.current_machine_passed is False
    assert report.cases[0].status == "not_run"
    current = next(entry for entry in report.platform_matrix if entry.current_machine)
    assert current.status == "not_run"


def test_acceptance_cli_case_filter_rejects_unknown_declared_case(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="Unknown acceptance case ID"):
        acceptance_main(
            [
                "--scenario",
                "config/acceptance/local-v1-primary-v1.json",
                "--output",
                str(tmp_path / "report.json"),
                "--case",
                "not-declared",
            ]
        )


def test_network_disabled_cli_requires_descriptive_operator_attestation(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="network-isolation-note"):
        acceptance_main(
            [
                "--scenario",
                "config/acceptance/local-v1-primary-v1.json",
                "--output",
                str(tmp_path / "report.json"),
                "--network-disabled",
            ]
        )


def test_independent_platform_cli_requires_exact_product_wheel(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="requires --product-wheel"):
        acceptance_main(
            [
                "--scenario",
                "config/acceptance/local-v1-platform-v1.json",
                "--output",
                str(tmp_path / "report.json"),
                "--network-disabled",
                "--network-isolation-note",
                "Host adapters disabled.",
            ]
        )


def test_filtered_case_run_is_diagnostic_and_cannot_pass_complete_suite() -> None:
    case = AcceptanceCase(
        case_id="only-case",
        kind="soak",
        command=(sys.executable, "-c", "raise SystemExit(0)"),
    )
    report = run_acceptance(
        cases=(case,),
        declared_case_ids=("only-case", "missing-case"),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        hardware=_hardware(),
    )

    assert report.suite_complete is False
    assert report.selected_case_ids == ("only-case",)
    assert report.current_machine_passed is False


def _complete_external_report(target_id: str) -> AcceptanceReport:
    suite = load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json"))
    system, architecture = target_id.split("-", maxsplit=1)
    target = detect_release_target(operating_system=system, architecture=architecture)
    process = ProcessEvidence(
        return_code=0,
        duration_seconds=1,
        stdout_tail="verified",
        stderr_tail="",
    )

    def process_for_case(case: AcceptanceCase) -> ProcessEvidence:
        if case.kind != "soak":
            return process
        assert case.duration_minutes is not None
        artifact = ProductionArtifactEvidence(
            path=Path("/independent-host/evidence.bin"), sha256="a" * 64, bytes=1
        )
        receipt = ProductionCaseReceipt(
            schema_version=2,
            evidence_scope="production_prompt_to_audio_case",
            generated_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
            run_id=UUID("21111111-1111-4111-8111-111111111111"),
            command=case.command,
            profile="standard",
            duration_minutes=case.duration_minutes,
            actual_duration_ms=case.duration_minutes * 60_000,
            quality_passed=True,
            artifacts={
                "run": artifact,
                "timeline": artifact,
                "audio": artifact,
                "manifest": artifact,
                "quality": artifact,
            },
        )
        return process.model_copy(
            update={
                "receipt_source_path": Path("/independent-host/receipt.json"),
                "receipt_sha256": "b" * 64,
                "production_receipt": receipt,
            }
        )

    def stage_marker(case: AcceptanceCase) -> AcceptanceStageMarker | None:
        if case.kind != "forced_interruption":
            return None
        stage = case.interruption_stage
        assert stage is not None
        return AcceptanceStageMarker(
            run_id=UUID("11111111-1111-4111-8111-111111111111"),
            stage=stage,
            process_id=42,
            observed_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
            durable_run_status="running",
            durable_run_stage={
                "synthesis": "synthesizing",
                "assembly": "assembling",
                "qc": "quality_check",
            }.get(stage, stage),
        )

    cases = tuple(
        AcceptanceCaseResult(
            case_id=case.case_id,
            kind=case.kind,
            passed=True,
            status="passed",
            stage=case.interruption_stage,
            attempts=tuple(process_for_case(case) for _ in range(case.repetitions)),
            interruption_observed=True if case.kind == "forced_interruption" else None,
            stage_marker=stage_marker(case),
            recovery=process if case.kind == "forced_interruption" else None,
            detail="verified",
        )
        for case in suite.cases
    )
    hardware = _hardware().model_copy(
        update={"operating_system": system, "architecture": architecture}
    )
    return AcceptanceReport(
        suite_id=suite.suite_id,
        suite_sha256=acceptance_suite_sha256(suite),
        selected_case_ids=tuple(case.case_id for case in suite.cases),
        product_version=__version__,
        product_wheel_sha256="c" * 64,
        generated_at=datetime(2026, 8, 5, 13, tzinfo=UTC),
        target=target,
        backend=select_friendly_backend(hardware),
        platform_matrix=build_platform_matrix(
            target,
            current_status="passed",
            current_evidence="Independent suite passed.",
        ),
        offline=_passing_offline(),
        migrations=_passing_migrations(),
        cases=cases,
        current_machine_passed=True,
        local_v1_release_ready=False,
    )


def test_suite_digest_binds_commands_and_repetitions_not_only_suite_id() -> None:
    suite = load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json"))
    changed_case = suite.cases[0].model_copy(update={"repetitions": suite.cases[0].repetitions + 1})
    changed = suite.model_copy(update={"cases": (changed_case, *suite.cases[1:])})

    assert acceptance_suite_sha256(changed) != acceptance_suite_sha256(suite)


def test_independent_platform_suite_is_bounded_real_production_smoke() -> None:
    suite = load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json"))

    assert suite.suite_scope == "independent_platform"
    assert len(suite.cases) == 1
    assert suite.cases[0].evidence_contract == "production_receipt_v2"
    assert suite.cases[0].duration_minutes == 1
    assert suite.cases[0].repetitions == 1


def test_external_aggregation_rejects_report_bound_to_different_suite_content(
    tmp_path: Path,
) -> None:
    report = _complete_external_report("linux-x86_64").model_copy(update={"suite_sha256": "a" * 64})
    report_path = tmp_path / "wrong-suite.json"
    write_acceptance_report(report_path, report)

    with pytest.raises(ValueError, match="exact suite content"):
        aggregate_platform_reports(
            suite=load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json")),
            report_paths=(report_path,),
        )


def test_external_evidence_aggregation_preserves_unrun_targets(tmp_path: Path) -> None:
    report_path = tmp_path / "windows.json"
    write_acceptance_report(report_path, _complete_external_report("windows-x86_64"))
    suite = load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json"))

    aggregate = aggregate_platform_reports(suite=suite, report_paths=(report_path,))

    assert aggregate.local_v1_release_ready is False
    assert aggregate.product_wheel_sha256 == "c" * 64
    assert [entry.status for entry in aggregate.platform_matrix].count("passed") == 1
    assert [entry.status for entry in aggregate.platform_matrix].count("not_run") == 4
    assert (
        aggregate.imported_reports[0].sha256 == hashlib.sha256(report_path.read_bytes()).hexdigest()
    )
    output = tmp_path / "aggregate.json"
    write_cross_platform_evidence(output, aggregate)
    assert json.loads(output.read_text(encoding="utf-8"))["local_v1_release_ready"] is False


def test_external_aggregation_rejects_legacy_report_without_product_wheel(
    tmp_path: Path,
) -> None:
    report = _complete_external_report("linux-x86_64").model_copy(
        update={"product_wheel_sha256": None}
    )
    report_path = tmp_path / "legacy-unbound.json"
    write_acceptance_report(report_path, report)

    with pytest.raises(ValueError, match="legacy/unbound"):
        aggregate_platform_reports(
            suite=load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json")),
            report_paths=(report_path,),
        )


def test_external_aggregation_rejects_mixed_same_version_product_wheels(
    tmp_path: Path,
) -> None:
    windows = tmp_path / "windows.json"
    linux = tmp_path / "linux.json"
    write_acceptance_report(windows, _complete_external_report("windows-x86_64"))
    write_acceptance_report(
        linux,
        _complete_external_report("linux-x86_64").model_copy(
            update={"product_wheel_sha256": "d" * 64}
        ),
    )

    with pytest.raises(ValueError, match="one product wheel SHA-256"):
        aggregate_platform_reports(
            suite=load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json")),
            report_paths=(windows, linux),
        )


def test_external_aggregation_and_status_bind_current_product_wheel(tmp_path: Path) -> None:
    report_path = tmp_path / "linux.json"
    write_acceptance_report(report_path, _complete_external_report("linux-x86_64"))
    suite = load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json"))

    platform_gate, _offline_gate, _observed = assess_platforms(
        suite=suite,
        platform_reports=(report_path,),
        expected_product_wheel_sha256="c" * 64,
    )
    assert platform_gate.outcome == "failed"  # The other four machines remain absent.
    assert platform_gate.facts["product_wheel_sha256"] == "c" * 64

    rejected, _offline_gate, observed = assess_platforms(
        suite=suite,
        platform_reports=(report_path,),
        expected_product_wheel_sha256="d" * 64,
    )
    assert rejected.outcome == "failed"
    assert "current packaged product wheel" in rejected.detail
    assert set(observed.values()) == {"not_run"}


def test_external_aggregation_accepts_verified_macos_process_isolation(
    tmp_path: Path,
) -> None:
    offline = OfflineAuditReport(
        passed=True,
        network_disabled_during_test=True,
        network_isolation_evidence_kind="macos_sandbox_exec",
        network_isolation_note="Verified macOS process sandbox.",
        process_network_isolation_enforced=True,
        network_isolation_evidence=_macos_sandbox_evidence(),
        required_pack_ids=("kokoro",),
        checks=(AuditCheck(check="fixture", passed=True, detail="verified"),),
    )
    report = _complete_external_report("darwin-arm64").model_copy(update={"offline": offline})
    report_path = tmp_path / "darwin-arm64.json"
    write_acceptance_report(report_path, report)

    aggregate = aggregate_platform_reports(
        suite=load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json")),
        report_paths=(report_path,),
    )

    current = next(
        entry for entry in aggregate.platform_matrix if entry.target.target_id == "darwin-arm64"
    )
    assert current.status == "passed"


def test_external_evidence_aggregation_rejects_partial_suite(tmp_path: Path) -> None:
    report = _complete_external_report("linux-x86_64")
    partial = report.model_copy(update={"cases": report.cases[:-1], "selected_case_ids": ()})
    report_path = tmp_path / "partial.json"
    write_acceptance_report(report_path, partial)

    with pytest.raises(ValueError, match="complete declared suite"):
        aggregate_platform_reports(
            suite=load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json")),
            report_paths=(report_path,),
        )


def test_external_evidence_aggregation_rejects_exit_zero_without_embedded_soak_receipt(
    tmp_path: Path,
) -> None:
    report = _complete_external_report("linux-x86_64")
    bare = ProcessEvidence(
        return_code=0,
        duration_seconds=1,
        stdout_tail="claimed success",
        stderr_tail="",
    )
    first = report.cases[0].model_copy(
        update={"attempts": tuple(bare for _ in report.cases[0].attempts)}
    )
    report = report.model_copy(update={"cases": (first, *report.cases[1:])})
    report_path = tmp_path / "missing-receipts.json"
    write_acceptance_report(report_path, report)

    with pytest.raises(ValueError, match="lacks embedded production receipts"):
        aggregate_platform_reports(
            suite=load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json")),
            report_paths=(report_path,),
        )


def test_external_evidence_aggregation_preserves_a_complete_failed_report(
    tmp_path: Path,
) -> None:
    report = _complete_external_report("linux-arm64")
    target = report.target
    failed = report.model_copy(
        update={
            "cases": (
                report.cases[0].model_copy(
                    update={"passed": False, "status": "failed", "attempts": ()}
                ),
                *report.cases[1:],
            ),
            "current_machine_passed": False,
            "platform_matrix": build_platform_matrix(
                target,
                current_status="failed",
                current_evidence="A soak case failed.",
            ),
        }
    )
    report_path = tmp_path / "failed.json"
    write_acceptance_report(report_path, failed)

    aggregate = aggregate_platform_reports(
        suite=load_acceptance_suite(Path("config/acceptance/local-v1-platform-v1.json")),
        report_paths=(report_path,),
    )

    linux_arm = next(
        entry for entry in aggregate.platform_matrix if entry.target.target_id == "linux-arm64"
    )
    assert linux_arm.status == "failed"
    assert aggregate.imported_reports[0].accepted is False


def test_doctor_refuses_windows_arm64_release_target() -> None:
    from whoopy.hardware import diagnose, load_runtime_profiles

    snapshot = _hardware().model_copy(
        update={"operating_system": "windows", "architecture": "arm64"}
    )
    result = diagnose(snapshot, load_runtime_profiles(Path("config/runtime_profiles.yaml")))

    assert result.supported is False
    assert result.selected_profile is None
