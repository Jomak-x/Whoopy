"""Aggregate complete independent Local V1 reports into one honest target matrix."""

from __future__ import annotations

import argparse
from pathlib import Path

from whoopy.release import (
    aggregate_platform_reports,
    load_acceptance_suite,
    write_cross_platform_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence = aggregate_platform_reports(
        suite=load_acceptance_suite(args.scenario),
        report_paths=tuple(args.report),
    )
    write_cross_platform_evidence(args.output, evidence)
    print(args.output.resolve())
    return 0 if evidence.local_v1_release_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
