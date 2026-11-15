#!/usr/bin/env python3
"""Offline synthetic ambience audition utility; no application integration or models."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Literal, cast

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "whoopy_ambience", ROOT / "src/whoopy/audio/ambience.py"
)
assert SPEC is not None and SPEC.loader is not None
AMBIENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AMBIENCE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    generate = actions.add_parser(
        "generate", help="Original procedural filtered noise, not recordings"
    )
    generate.add_argument("--output", type=Path, required=True, help="Fresh output directory")
    generate.add_argument("--kind", choices=["rain", "ocean"], default="rain")
    generate.add_argument("--seconds", type=float, default=60)
    generate.add_argument("--seed", type=int, default=17)
    mix = actions.add_parser("mix", help="Derive stereo mix from explicit local PCM WAV inputs")
    mix.add_argument("--output", type=Path, required=True, help="Fresh output directory")
    mix.add_argument("--narration", type=Path, required=True)
    mix.add_argument("--bed", type=Path, required=True)
    mix.add_argument("--narration-gain-db", type=float, default=-3)
    mix.add_argument("--bed-gain-db", type=float, default=-18)
    args = parser.parse_args()
    try:
        if args.action == "generate":
            result = AMBIENCE.generate_bed(
                args.output,
                kind=cast(Literal["rain", "ocean"], args.kind),
                seconds=args.seconds,
                seed=args.seed,
            )
        else:
            result = AMBIENCE.mix_bed(
                args.output,
                narration=args.narration,
                bed=args.bed,
                narration_gain_db=args.narration_gain_db,
                bed_gain_db=args.bed_gain_db,
            )
    except (AMBIENCE.AmbienceError, OSError) as error:
        print(f"Ambience preparation failed: {error}", file=sys.stderr)
        return 1
    print(f"Saved {result}\nQuality: unrated until human listening. See manifest.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
