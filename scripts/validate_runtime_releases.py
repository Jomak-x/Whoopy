"""Validate runtime recipes and ensure every published bundle has build inputs."""

from __future__ import annotations

import argparse
from pathlib import Path

from whoopy.model_packs.operations import ModelPackOperationError
from whoopy.model_packs.runtime_builder import validate_runtime_releases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-manifest", type=Path, default=Path("config/runtime_packs.yaml"))
    parser.add_argument(
        "--build-recipes", type=Path, default=Path("config/runtime_build_recipes.yaml")
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        messages = validate_runtime_releases(
            args.runtime_manifest, args.build_recipes, args.repository_root
        )
    except (ModelPackOperationError, OSError, ValueError) as error:
        parser.error(str(error))
    print("\n".join(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
