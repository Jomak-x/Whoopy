"""Generate the fail-closed consolidated Local V1 release-status report."""

from __future__ import annotations

import argparse
from pathlib import Path

from whoopy.release import load_acceptance_suite
from whoopy.release.status import (
    build_local_v1_acceptance_status,
    write_local_v1_acceptance_status,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument(
        "--corpus-report",
        type=Path,
        default=Path("evaluations/local/corpus-acceptance-current.json"),
    )
    parser.add_argument(
        "--soak-report",
        type=Path,
        default=Path("acceptance-reports/soak-exports-2026-08-05.json"),
    )
    parser.add_argument(
        "--interruption-report",
        type=Path,
        default=Path("acceptance-reports/forced-interruption-local-2026-08-05.json"),
    )
    parser.add_argument(
        "--studio-lifecycle-report",
        type=Path,
        default=Path("evaluations/local/studio-lifecycle-current.json"),
    )
    parser.add_argument("--model-registry", type=Path, default=Path("config/model_packs.yaml"))
    parser.add_argument("--models-root", type=Path, default=Path("models"))
    parser.add_argument("--lab-root", type=Path, default=Path("runs/.whoopy-lab"))
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--scenario",
        type=Path,
        default=Path("config/acceptance/local-v1-platform-v1.json"),
    )
    evidence = parser.add_mutually_exclusive_group()
    evidence.add_argument(
        "--platform-report",
        type=Path,
        action="append",
        default=[],
        help="Independent full acceptance report; repeat once per target.",
    )
    evidence.add_argument(
        "--cross-platform-evidence",
        type=Path,
        help="Strict aggregate whose source reports remain available and hash-identical.",
    )
    parser.add_argument(
        "--ci-portability-summary",
        type=Path,
        help="Optional informational CI summary; it can never satisfy platform acceptance.",
    )
    parser.add_argument(
        "--installed-bundle-report",
        type=Path,
        action="append",
        default=[],
        help="Fresh installed-bundle offline lifecycle receipt; repeat once per target.",
    )
    parser.add_argument(
        "--verify-model-digests",
        action="store_true",
        help="Hash every Studio model file; without this the model gate stays unverified.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("acceptance-reports/local-v1-status-current.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    status = build_local_v1_acceptance_status(
        project_root=args.project_root.resolve(),
        corpus_report=args.corpus_report,
        soak_report=args.soak_report,
        interruption_report=args.interruption_report,
        studio_lifecycle_report=args.studio_lifecycle_report,
        model_registry=args.model_registry,
        models_root=args.models_root,
        lab_root=args.lab_root,
        acceptance_suite=load_acceptance_suite(args.scenario),
        dist_directory=args.dist_dir,
        installed_bundle_reports=tuple(args.installed_bundle_report),
        platform_reports=tuple(args.platform_report),
        cross_platform_evidence=args.cross_platform_evidence,
        ci_portability_summary=args.ci_portability_summary,
        verify_model_digests=args.verify_model_digests,
    )
    write_local_v1_acceptance_status(args.output, status)
    print(args.output.resolve())
    for gate in status.gates:
        print(f"{gate.outcome:10} {gate.gate_id}: {gate.detail}")
    return 0 if status.local_v1_release_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
