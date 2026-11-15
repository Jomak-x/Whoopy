"""Fail when the checked-in local frontend differs from a fresh Svelte build."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def changed_bundle_paths(repository: Path) -> tuple[str, ...]:
    """Return tracked or untracked bundled assets changed by the preceding build."""

    bundle = "src/whoopy/webui/static"
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--", bundle],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", bundle],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return tuple(sorted({path for path in (*tracked, *untracked) if path}))


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    changed = changed_bundle_paths(repository)
    if not changed:
        print("Bundled Svelte output matches the checked-in product assets.")
        return 0
    print(
        "Fresh frontend build changed bundled product assets. "
        "Run `npm run build` in web/ and commit the complete static output:",
        file=sys.stderr,
    )
    for path in changed:
        print(f"  {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
