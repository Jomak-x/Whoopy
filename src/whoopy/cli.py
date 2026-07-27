"""Command-line entry point for Whoopy's foundation commands."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from whoopy.config import ConfigError, load_settings
from whoopy.control import LocalControlPlane
from whoopy.hardware import diagnose, inspect_hardware, load_runtime_profiles
from whoopy.pipeline import LocalWorker, RunRecord, RunStore
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
    if record.error is not None:
        print(f"Error: {record.error}")


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

    worker_parser = subcommands.add_parser(
        "worker",
        help="Process queued work outside the control-plane command.",
    )
    worker_commands = worker_parser.add_subparsers(dest="worker_command")
    process_parser = worker_commands.add_parser(
        "process",
        help="Process one queued run and write its timeline artifact.",
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

    if args.command == "worker" and args.worker_command == "process":
        try:
            store = _run_store(args)
            record = LocalWorker(store).process(args.run_id)
        except (ConfigError, RunStoreError, WorkerError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    parser.print_help()
    return 0
