"""Command-line entry point for Serenity's foundation commands."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from serenity.config import ConfigError, load_settings
from serenity.hardware import diagnose, inspect_hardware, load_runtime_profiles


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serenity",
        description="Local-first, timeline-driven meditation generation.",
    )
    subcommands = parser.add_subparsers(dest="command")

    config_parser = subcommands.add_parser("config", help="Inspect Serenity configuration.")
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
            print("Serenity native compatibility check")
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

    parser.print_help()
    return 0
