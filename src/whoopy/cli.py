"""Command-line entry point for Whoopy's foundation commands."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from whoopy.artifacts import (
    ArtifactError,
    ArtifactInstaller,
    ArtifactSpec,
    ArtifactState,
    ArtifactStore,
    TargetPlatform,
    load_artifact_lock,
)
from whoopy.config import ConfigError, load_settings
from whoopy.control import LocalControlPlane
from whoopy.hardware import DoctorResult, diagnose, inspect_hardware, load_runtime_profiles
from whoopy.pipeline import LocalWorker, RunRecord, RunStore, SegmentCache
from whoopy.pipeline.runs import RunStoreError
from whoopy.pipeline.worker import WorkerError


def _add_run_location_arguments(parser: argparse.ArgumentParser) -> None:
    """Give run commands the same configurable storage contract."""

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Directory containing default.yaml and optional local.yaml.",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        help="Override pipeline.checkpoint_dir for this command.",
    )


def _run_store(args: argparse.Namespace) -> RunStore:
    """Resolve the run root without duplicating config logic in each command."""

    if args.runs_dir is not None:
        return RunStore(args.runs_dir)
    settings = load_settings(args.config_dir)
    return RunStore(settings.pipeline.checkpoint_dir)


def _segment_cache(store: RunStore) -> SegmentCache:
    """Share cached speech across every run stored beneath this root."""

    return SegmentCache(store.root / ".cache" / "segments")


def _add_model_location_arguments(parser: argparse.ArgumentParser) -> None:
    """Give model commands explicit, machine-local storage inputs."""

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Directory containing runtime_profiles.yaml.",
    )
    parser.add_argument(
        "--artifact-lock",
        type=Path,
        help="Override CONFIG_DIR/artifacts.yaml.",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("models/managed"),
        help="Ignored directory for verified downloads and extracted runtimes.",
    )


def _model_plan(
    args: argparse.Namespace,
) -> tuple[DoctorResult, TargetPlatform, ArtifactStore, list[ArtifactSpec]]:
    """Resolve a safe profile and its exact artifacts without loading a model."""

    lock_path = args.artifact_lock or args.config_dir / "artifacts.yaml"
    artifact_lock = load_artifact_lock(lock_path)
    profiles = [
        profile
        for profile in load_runtime_profiles(args.config_dir / "runtime_profiles.yaml")
        if profile.name in artifact_lock.profiles
    ]
    if args.profile != "auto" and args.profile not in artifact_lock.profiles:
        raise ArtifactError(
            f"Profile {args.profile!r} has no pinned artifact plan yet. "
            f"Available plans: {', '.join(sorted(artifact_lock.profiles))}."
        )
    inspection_path = args.models_dir
    while not inspection_path.exists() and inspection_path != inspection_path.parent:
        inspection_path = inspection_path.parent
    snapshot = inspect_hardware(inspection_path)
    result = diagnose(snapshot, profiles, args.profile)
    target = TargetPlatform(
        operating_system=snapshot.operating_system,
        architecture=snapshot.architecture,
    )
    if not result.supported or result.selected_profile is None:
        return result, target, ArtifactStore(args.models_dir), []
    artifacts = artifact_lock.resolve(result.selected_profile.name, target)
    return result, target, ArtifactStore(args.models_dir), artifacts


def _print_run(record: RunRecord, store: RunStore, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"Run: {record.run_id}")
    print(f"Status: {record.status.value}")
    print(f"Prompt: {record.prompt}")
    print(f"Record: {store.record_path(record.run_id)}")
    if record.timeline_artifact is not None:
        print(f"Timeline: {store.timeline_path(record.run_id)}")
    if record.audio_artifact is not None:
        print(f"Audio: {store.audio_path(record.run_id)}")
    if record.audio_manifest_artifact is not None:
        print(f"Audio manifest: {store.audio_manifest_path(record.run_id)}")
    if record.quality_artifact is not None:
        print(f"Quality report: {store.quality_path(record.run_id)}")
    if record.error is not None:
        print(f"Error: {record.error}")
    if record.recovery is not None:
        recovery = record.recovery
        print(
            "Recovery: "
            f"{recovery.speech_segments_completed}/{recovery.speech_segments_total} speech, "
            f"{recovery.cache_hits} cache hits, "
            f"{recovery.checkpoint_reuses} checkpoint reuses, "
            f"{recovery.resume_count} resumes"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whoopy",
        description="Local-first, timeline-driven meditation generation.",
    )
    subcommands = parser.add_subparsers(dest="command")

    config_parser = subcommands.add_parser("config", help="Inspect Whoopy configuration.")
    config_commands = config_parser.add_subparsers(dest="config_command")
    show_parser = config_commands.add_parser(
        "show",
        help="Print the resolved configuration after applying all overrides.",
    )
    show_parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Directory containing default.yaml and optional local.yaml.",
    )
    # These flags establish CLI as the highest-priority config layer without
    # pretending that the generation commands from later phases already exist.
    show_parser.add_argument("--llm-backend")
    show_parser.add_argument("--tts-backend")
    show_parser.add_argument("--tts-voice")

    doctor_parser = subcommands.add_parser(
        "doctor",
        help="Inspect this laptop and recommend the highest safe local profile.",
    )
    doctor_parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Directory containing runtime_profiles.yaml.",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output for installers and the future UI.",
    )
    doctor_parser.add_argument(
        "--profile",
        choices=("auto", "basic", "lite", "standard", "high", "studio"),
        help="Test a named profile instead of the configured automatic selection.",
    )

    models_parser = subcommands.add_parser(
        "models",
        help="Inspect or install verified local model artifacts.",
    )
    model_commands = models_parser.add_subparsers(dest="models_command")
    models_list_parser = model_commands.add_parser(
        "list",
        help="List locked artifacts for this operating system without loading them.",
    )
    models_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable artifact metadata and state.",
    )
    _add_model_location_arguments(models_list_parser)

    models_doctor_parser = model_commands.add_parser(
        "doctor",
        help="Resolve a safe profile and report everything it needs.",
    )
    models_doctor_parser.add_argument(
        "--profile",
        choices=("auto", "basic", "lite", "standard", "high", "studio"),
        default="auto",
        help="Resolve a named profile or choose automatically.",
    )
    models_doctor_parser.add_argument(
        "--verify",
        action="store_true",
        help="Rehash complete downloads, including multi-gigabyte model files.",
    )
    models_doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable hardware, plan, and artifact state.",
    )
    _add_model_location_arguments(models_doctor_parser)

    models_install_parser = model_commands.add_parser(
        "install",
        help="Install one safe profile from verified offline files or HTTPS.",
    )
    models_install_parser.add_argument(
        "--profile",
        choices=("auto", "basic", "lite", "standard", "high", "studio"),
        default="auto",
        help="Install a named profile or choose automatically.",
    )
    models_install_parser.add_argument(
        "--offline-dir",
        type=Path,
        help="Search this directory recursively for already downloaded files.",
    )
    models_install_parser.add_argument(
        "--no-network",
        action="store_true",
        help="Fail instead of downloading an artifact missing from offline storage.",
    )
    models_install_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable install report.",
    )
    _add_model_location_arguments(models_install_parser)

    run_parser = subcommands.add_parser("run", help="Create or inspect durable local runs.")
    run_commands = run_parser.add_subparsers(dest="run_command")
    create_parser = run_commands.add_parser(
        "create",
        help="Save a prompt as a queued run without processing it inline.",
    )
    create_parser.add_argument("prompt", help="Prompt to save in the run record.")
    create_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable run record.",
    )
    _add_run_location_arguments(create_parser)

    run_show_parser = run_commands.add_parser("show", help="Print a saved run record.")
    run_show_parser.add_argument("run_id", help="UUID printed by `whoopy run create`.")
    run_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable run record.",
    )
    _add_run_location_arguments(run_show_parser)

    run_resume_parser = run_commands.add_parser(
        "resume",
        help="Resume a failed or interrupted run from verified segment checkpoints.",
    )
    run_resume_parser.add_argument("run_id", help="UUID of a failed or running run.")
    run_resume_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable completed run record.",
    )
    _add_run_location_arguments(run_resume_parser)

    cache_parser = subcommands.add_parser(
        "cache",
        help="Inspect the local content-addressed speech cache.",
    )
    cache_commands = cache_parser.add_subparsers(dest="cache_command")
    cache_stats_parser = cache_commands.add_parser(
        "stats",
        help="Count valid and corrupt entries without changing the cache.",
    )
    cache_stats_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only machine-readable cache statistics.",
    )
    _add_run_location_arguments(cache_stats_parser)

    worker_parser = subcommands.add_parser(
        "worker",
        help="Process queued work outside the control-plane command.",
    )
    worker_commands = worker_parser.add_subparsers(dest="worker_command")
    process_parser = worker_commands.add_parser(
        "process",
        help="Process one queued run and write its timeline and WAV artifacts.",
    )
    process_parser.add_argument("run_id", help="UUID printed by `whoopy run create`.")
    process_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable completed run record.",
    )
    _add_run_location_arguments(process_parser)

    return parser


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for section, field in (
        ("llm", "backend"),
        ("tts", "backend"),
        ("tts", "voice"),
    ):
        value = getattr(args, f"{section}_{field}")
        if value is not None:
            overrides.setdefault(section, {})[field] = value
    return overrides


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "config" and args.config_command == "show":
        try:
            settings = load_settings(args.config_dir, cli_overrides=_cli_overrides(args))
        except ConfigError as error:
            parser.error(str(error))
        print(yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False), end="")
        return 0

    if args.command == "doctor":
        try:
            settings = load_settings(args.config_dir)
            profiles = load_runtime_profiles(args.config_dir / "runtime_profiles.yaml")
            requested_profile = args.profile or settings.hardware.profile
            result = diagnose(inspect_hardware(), profiles, requested_profile)
        except ConfigError as error:
            parser.error(str(error))
        if args.json:
            print(json.dumps(result.model_dump(mode="json"), indent=2))
        else:
            snapshot = result.snapshot
            print("Whoopy native compatibility check")
            print(f"  System: {snapshot.operating_system} {snapshot.architecture}")
            print(f"  CPU threads: {snapshot.cpu_count}")
            print(
                f"  Memory: {snapshot.available_ram_gb:g} GB available / "
                f"{snapshot.total_ram_gb:g} GB total"
            )
            print(f"  Free disk: {snapshot.free_disk_gb:g} GB")
            print(f"  Detected acceleration: {', '.join(snapshot.accelerators)}")
            print(f"  Result: {'supported' if result.supported else 'not currently supported'}")
            if result.selected_profile is not None:
                print(f"  Recommended profile: {result.selected_profile.name}")
            for message in result.messages:
                print(f"  - {message}")
        return 0 if result.supported else 2

    if args.command == "models" and args.models_command == "list":
        try:
            lock_path = args.artifact_lock or args.config_dir / "artifacts.yaml"
            artifact_lock = load_artifact_lock(lock_path)
            target = TargetPlatform.current()
            artifact_store = ArtifactStore(args.models_dir)
            artifacts = [
                artifact for artifact in artifact_lock.artifacts if artifact.supports(target)
            ]
            statuses = [artifact_store.inspect(artifact) for artifact in artifacts]
        except ArtifactError as error:
            parser.error(str(error))
        if args.json:
            print(
                json.dumps(
                    {
                        "target": target.model_dump(mode="json"),
                        "artifacts": [status.model_dump(mode="json") for status in statuses],
                    },
                    indent=2,
                )
            )
        else:
            print(f"Whoopy locked artifacts for {target.operating_system} {target.architecture}")
            for status in statuses:
                print(
                    f"  [{status.state.value:9}] {status.artifact_id} "
                    f"({status.license_id}, {status.size_bytes} bytes)"
                )
        return 0

    if args.command == "models" and args.models_command == "doctor":
        try:
            result, target, artifact_store, artifacts = _model_plan(args)
            statuses = [
                artifact_store.inspect(artifact, verify_digest=args.verify)
                for artifact in artifacts
            ]
        except (ArtifactError, ConfigError) as error:
            parser.error(str(error))
        if args.json:
            print(
                json.dumps(
                    {
                        "hardware": result.model_dump(mode="json"),
                        "target": target.model_dump(mode="json"),
                        "artifacts": [status.model_dump(mode="json") for status in statuses],
                        "ready": bool(statuses)
                        and all(status.state is ArtifactState.INSTALLED for status in statuses),
                        "loaded_models": False,
                    },
                    indent=2,
                )
            )
        else:
            print("Whoopy model compatibility check")
            print(f"  Target: {target.operating_system} {target.architecture}")
            print(f"  Hardware: {'supported' if result.supported else 'not supported'}")
            if result.selected_profile is not None:
                print(f"  Profile: {result.selected_profile.name}")
            for message in result.messages:
                print(f"  - {message}")
            for status in statuses:
                print(f"  [{status.state.value:9}] {status.display_name}")
            print("  No model was loaded.")
        if not result.supported:
            return 2
        return (
            0
            if statuses and all(status.state is ArtifactState.INSTALLED for status in statuses)
            else 1
        )

    if args.command == "models" and args.models_command == "install":
        try:
            result, target, artifact_store, artifacts = _model_plan(args)
            if not result.supported or result.selected_profile is None:
                for message in result.messages:
                    print(message)
                return 2
            callback = None if args.json else lambda message: print(f"  {message}")
            if not args.json:
                print(
                    f"Installing Whoopy {result.selected_profile.name} artifacts for "
                    f"{target.operating_system} {target.architecture}"
                )
            report = ArtifactInstaller(
                artifact_store,
                status_callback=callback,
            ).install_profile(
                result.selected_profile.name,
                artifacts,
                target,
                offline_directory=args.offline_dir,
                allow_network=not args.no_network,
            )
        except (ArtifactError, ConfigError) as error:
            parser.error(str(error))
        if args.json:
            print(report.model_dump_json(indent=2))
        else:
            print(
                f"Ready: {len(report.artifacts)} artifacts "
                f"({len(report.installed)} installed, {len(report.reused)} reused)"
            )
            print("No model was loaded.")
        return 0

    if args.command == "run" and args.run_command == "create":
        try:
            store = _run_store(args)
            record = LocalControlPlane(store).submit_prompt(args.prompt)
        except (ConfigError, RunStoreError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        if not args.json:
            next_command = f"whoopy worker process {record.run_id}"
            if args.runs_dir is not None:
                next_command += f' --runs-dir "{args.runs_dir}"'
            elif args.config_dir != Path("config"):
                next_command += f' --config-dir "{args.config_dir}"'
            print(f"Next: {next_command}")
        return 0

    if args.command == "run" and args.run_command == "show":
        try:
            store = _run_store(args)
            record = LocalControlPlane(store).get_run(args.run_id)
        except (ConfigError, RunStoreError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    if args.command == "run" and args.run_command == "resume":
        try:
            store = _run_store(args)
            record = LocalWorker(store, cache=_segment_cache(store)).resume(args.run_id)
        except (ConfigError, RunStoreError, WorkerError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    if args.command == "cache" and args.cache_command == "stats":
        try:
            store = _run_store(args)
            stats = _segment_cache(store).stats()
        except (ConfigError, RunStoreError) as error:
            parser.error(str(error))
        if args.json:
            print(stats.model_dump_json(indent=2))
        else:
            print("Whoopy speech cache")
            print(f"  Entries: {stats.entries}")
            print(f"  Valid: {stats.valid_entries}")
            print(f"  Corrupt: {stats.corrupt_entries}")
            print(f"  Audio bytes: {stats.audio_bytes}")
        return 0

    if args.command == "worker" and args.worker_command == "process":
        try:
            store = _run_store(args)
            record = LocalWorker(store, cache=_segment_cache(store)).process(args.run_id)
        except (ConfigError, RunStoreError, WorkerError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    parser.print_help()
    return 0
