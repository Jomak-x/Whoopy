from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_frontend_bundle import changed_bundle_paths


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def test_bundle_drift_finds_modified_and_new_hashed_assets(tmp_path: Path) -> None:
    bundle = tmp_path / "src/whoopy/webui/static"
    bundle.mkdir(parents=True)
    index = bundle / "index.html"
    index.write_text("original", encoding="utf-8")
    _git(tmp_path, "init")
    _git(tmp_path, "add", "src/whoopy/webui/static/index.html")
    _git(
        tmp_path,
        "-c",
        "user.name=Whoopy Test",
        "-c",
        "user.email=whoopy@example.invalid",
        "commit",
        "-m",
        "fixture",
    )

    assert changed_bundle_paths(tmp_path) == ()

    index.write_text("rebuilt", encoding="utf-8")
    generated = bundle / "_app/immutable/nodes/2.hash.js"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated", encoding="utf-8")

    assert changed_bundle_paths(tmp_path) == (
        "src/whoopy/webui/static/_app/immutable/nodes/2.hash.js",
        "src/whoopy/webui/static/index.html",
    )
