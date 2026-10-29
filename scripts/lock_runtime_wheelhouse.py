"""Generate an exact deterministic lock from one reviewed target wheelhouse."""

from __future__ import annotations

import argparse
from pathlib import Path

from whoopy.model_packs.operations import ModelPackOperationError
from whoopy.model_packs.runtime_builder import lock_runtime_wheelhouse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheelhouse", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        payload = lock_runtime_wheelhouse(args.wheelhouse)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(payload)
    except (ModelPackOperationError, OSError) as error:
        parser.error(str(error))
    print(f"Wrote {len(payload.splitlines())} exact wheel hashes to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
