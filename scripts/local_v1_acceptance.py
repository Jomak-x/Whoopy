"""Generate one honest, machine-readable Local V1 acceptance report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import BadZipFile, ZipFile

REPOSITORY_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if REPOSITORY_IMPORT_ROOT not in sys.path:
    sys.path.insert(0, REPOSITORY_IMPORT_ROOT)

import whoopy  # noqa: E402
from scripts.offline_bundle_acceptance import (  # noqa: E402
    MACOS_SANDBOX_ACTIVE,
    _run_in_macos_network_sandbox,
    _verify_macos_sandbox_isolation,
)
from whoopy.release import (  # noqa: E402
    acceptance_suite_sha256,
    audit_offline_post_acquisition,
    inspect_migrations,
    load_acceptance_suite,
    run_acceptance,
    write_acceptance_report,
)
from whoopy.release.acceptance import _offline_environment  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--models-root", type=Path, default=Path("models"))
    parser.add_argument(
        "--product-wheel",
        type=Path,
        help=(
            "Exact shared Whoopy wheel under test. Required by independent-platform suites; "
            "its SHA-256 is embedded in the receipt."
        ),
    )
    parser.add_argument("--required-pack", action="append", default=["kokoro"])
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help=(
            "Run only this declared case ID (repeatable); the declaration is validated but the "
            "filtered report cannot pass the complete suite."
        ),
    )
    parser.add_argument(
        "--network-disabled",
        action="store_true",
        help="Attest that networking was disabled outside Whoopy for the complete run.",
    )
    parser.add_argument(
        "--network-isolation-note",
        help="Describe the external OS control; required with --network-disabled.",
    )
    parser.add_argument(
        "--macos-sandbox-network-isolation",
        action="store_true",
        help=(
            "Run the complete declared suite with external networking denied by a probed "
            "macOS process sandbox while preserving localhost."
        ),
    )
    return parser


def _verified_product_wheel_sha256(product_wheel: Path) -> str:
    with product_wheel.open("rb") as wheel_file:
        digest = hashlib.file_digest(wheel_file, "sha256").hexdigest()
        with ZipFile(wheel_file) as wheel:
            wheel_members = wheel.namelist()
            package_members = {
                name
                for name in wheel_members
                if name.startswith("whoopy/") and not name.endswith("/")
            }
            if "whoopy/__init__.py" not in package_members:
                raise ValueError("wheel has no Whoopy package")
            worker_names = []
            for name in wheel_members:
                if name.endswith("/"):
                    continue
                _, separator, relative = name.partition(".data/data/share/whoopy/scripts/")
                if separator:
                    worker_names.append(relative)
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json, sys; from pathlib import Path; import whoopy; "
                    "from whoopy.locations import default_config_directory, runtime_worker_script; "
                    "worker_names = json.loads(sys.argv[1]); "
                    "print(json.dumps({'package': str(Path(whoopy.__file__).parent.resolve()), "
                    "'config': str(default_config_directory().resolve()), "
                    "'workers': {name: str(runtime_worker_script(name).resolve()) "
                    "for name in worker_names}}))",
                    json.dumps(worker_names),
                ],
                env=_offline_environment(),
                capture_output=True,
                text=True,
                check=True,
            )
            identity = json.loads(probe.stdout)
            package = Path(identity["package"])
            config = Path(identity["config"])
            if package != Path(whoopy.__file__).resolve().parent:
                raise ValueError("acceptance driver and child resolve different Whoopy packages")
            actual_members = {
                "whoopy/" + path.relative_to(package).as_posix()
                for path in package.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
            }
            if actual_members != package_members:
                raise ValueError("executed package file set differs from the wheel")
            resources: dict[str, Path] = {
                name: package / name.removeprefix("whoopy/") for name in package_members
            }
            for name in wheel_members:
                if name.endswith("/"):
                    continue
                _, separator, relative = name.partition(".data/data/share/whoopy/")
                if not separator:
                    continue
                if relative.startswith("config/"):
                    resources[name] = config / relative.removeprefix("config/")
                elif relative.startswith("scripts/"):
                    worker = relative.removeprefix("scripts/")
                    resources[name] = Path(identity["workers"][worker])
            if not any(
                name.endswith(".data/data/share/whoopy/config/default.yaml") for name in resources
            ):
                raise ValueError("wheel has no bundled default configuration")
            for name, path in resources.items():
                if not path.is_file() or path.read_bytes() != wheel.read(name):
                    raise ValueError(f"executed product differs from wheel member {name}")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.network_disabled and args.macos_sandbox_network_isolation:
        raise SystemExit(
            "--network-disabled and --macos-sandbox-network-isolation are mutually exclusive"
        )
    if args.network_disabled and not (
        args.network_isolation_note and args.network_isolation_note.strip()
    ):
        raise SystemExit("--network-disabled requires --network-isolation-note")
    if args.network_isolation_note and not args.network_disabled:
        raise SystemExit("--network-isolation-note requires --network-disabled")
    if args.macos_sandbox_network_isolation and os.environ.get(MACOS_SANDBOX_ACTIVE) != "1":
        child_argv = list(argv if argv is not None else sys.argv[1:])
        try:
            return _run_in_macos_network_sandbox(child_argv, script=Path(__file__))
        except (OSError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    try:
        network_isolation_evidence = (
            _verify_macos_sandbox_isolation() if args.macos_sandbox_network_isolation else None
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    suite = load_acceptance_suite(args.scenario)
    product_wheel_sha256 = None
    if suite.suite_scope == "independent_platform" and args.product_wheel is None:
        raise SystemExit("Independent-platform acceptance requires --product-wheel")
    if args.product_wheel is not None:
        product_wheel = args.product_wheel.expanduser().resolve()
        if (
            not product_wheel.is_file()
            or not product_wheel.name.startswith("whoopy-")
            or product_wheel.suffix != ".whl"
        ):
            raise SystemExit(f"--product-wheel is not a Whoopy wheel: {product_wheel}")
        try:
            product_wheel_sha256 = _verified_product_wheel_sha256(product_wheel)
        except (OSError, ValueError, BadZipFile, subprocess.SubprocessError) as error:
            raise SystemExit(
                f"--product-wheel does not match the executed product: {error}"
            ) from error
    cases = suite.cases
    if args.case_ids:
        requested = tuple(dict.fromkeys(args.case_ids))
        known = {case.case_id: case for case in suite.cases}
        unknown = [case_id for case_id in requested if case_id not in known]
        if unknown:
            raise SystemExit(f"Unknown acceptance case ID(s): {', '.join(unknown)}")
        cases = tuple(known[case_id] for case_id in requested)
    offline = audit_offline_post_acquisition(
        config_directory=args.config_dir,
        models_root=args.models_root,
        required_pack_ids=tuple(dict.fromkeys(args.required_pack)),
        network_disabled_during_test=args.network_disabled,
        network_isolation_note=args.network_isolation_note,
        network_isolation_evidence=network_isolation_evidence,
    )
    migrations = inspect_migrations(
        runs_directory=args.runs_dir,
        config_directory=args.config_dir,
        models_root=args.models_root,
    )
    report = run_acceptance(
        cases=cases,
        offline=offline,
        migrations=migrations,
        suite_id=suite.suite_id,
        suite_sha256=acceptance_suite_sha256(suite),
        declared_case_ids=tuple(case.case_id for case in suite.cases),
        product_wheel_sha256=product_wheel_sha256,
    )
    write_acceptance_report(args.output, report)
    print(args.output.resolve())
    return 0 if report.current_machine_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
