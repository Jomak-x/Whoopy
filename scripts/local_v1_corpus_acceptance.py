#!/usr/bin/env python3
"""Report the Local V1 ten-prompt quality gate from durable local evidence."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from whoopy.evaluation import load_evaluation_set
from whoopy.evaluation.acceptance import assess_corpus_acceptance, load_lab_acceptance_evidence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evaluation-set",
        type=Path,
        default=Path("config/evaluation/local-v1.yaml"),
    )
    parser.add_argument("--lab-root", type=Path, default=Path("runs/.whoopy-lab"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    evaluation_set = load_evaluation_set(args.evaluation_set)
    evidence = load_lab_acceptance_evidence(args.lab_root)
    summary = assess_corpus_acceptance(evaluation_set, evidence)
    payload = summary.model_dump_json(indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if summary.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
