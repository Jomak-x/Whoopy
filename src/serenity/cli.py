"""Command-line entry point for Serenity's foundation commands."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from serenity.config import ConfigError, load_settings


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

    parser.print_help()
    return 0
