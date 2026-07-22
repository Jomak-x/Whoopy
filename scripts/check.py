"""Run Serenity's platform-neutral quality gate.

Make remains a convenience wrapper for Unix contributors; CI and Windows call
this script through uv so the actual verification contract is identical.
"""

from __future__ import annotations

import subprocess
import sys

CHECKS = (
    ("lint", [sys.executable, "-m", "ruff", "check", "."]),
    ("format", [sys.executable, "-m", "ruff", "format", "--check", "."]),
    ("types", [sys.executable, "-m", "mypy", "src", "tests", "scripts"]),
    ("tests", [sys.executable, "-m", "pytest"]),
)


def main() -> int:
    """Stop at the first failed check and preserve its process exit code."""

    for name, command in CHECKS:
        print(f"==> {name}", flush=True)
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
