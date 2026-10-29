"""Build one reviewed offline runtime release; never packages an existing venv."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from whoopy.model_packs.operations import ModelPackOperationError
from whoopy.model_packs.runtime_builder import (
    build_runtime_bundle,
    bundle_manifest_snippet,
    load_runtime_build_recipes,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_id")
    parser.add_argument("--target", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipes", type=Path, default=Path("config/runtime_build_recipes.yaml"))
    args = parser.parse_args()
    try:
        recipes = load_runtime_build_recipes(args.recipes)
        recipe = next(item for item in recipes.runtimes if item.runtime_id == args.runtime_id)
        result = build_runtime_bundle(
            recipe,
            source=args.source,
            requirements_lock=args.lock,
            wheelhouse=args.wheelhouse,
            output_directory=args.output,
            target=args.target,
        )
    except StopIteration:
        parser.error(f"Unknown runtime build recipe: {args.runtime_id}")
    except (ModelPackOperationError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    print("\nReviewed config/runtime_packs.yaml bundle entry:\n")
    print(bundle_manifest_snippet(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
