from __future__ import annotations

from pathlib import Path

import pytest

from whoopy.model_packs import ModelPackOperationError
from whoopy.model_packs.runtime_builder import validate_runtime_releases


def test_current_runtime_release_declarations_validate_but_report_real_blockers() -> None:
    messages = validate_runtime_releases(
        Path("config/runtime_packs.yaml"),
        Path("config/runtime_build_recipes.yaml"),
        Path("."),
    )

    assert any(message.startswith("fish-speech: published") for message in messages)
    assert any(message.startswith("moss-tts: partially published") for message in messages)
    assert any(
        "qwen3-tts: published — all declared targets have reviewed locks and published bundles"
        in message
        for message in messages
    )
    assert any(
        message
        == "fish-speech: published — all declared targets have reviewed locks and published "
        "bundles"
        for message in messages
    )
    assert any(
        message == ("moss-tts: partially published — missing reviewed locks for linux-x86_64")
        for message in messages
    )


def test_published_bundle_requires_declared_target_lock(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        """schema_version: 1
runtimes:
  - runtime_id: example
    display_name: Example
    revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    upstream_repository: https://example.test/source
    upstream_revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    python_version: 3.12.12
    license_id: Apache-2.0
    managed_directory: managed/runtimes/example
    required_markers: [.whoopy-runtime.json, .venv/pyvenv.cfg]
    bundles:
      - target: linux-x86_64
        filename: example.tar.gz
        size_bytes: 1
        sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
        archive: tar.gz
        install_mode: locked-wheelhouse
""",
        "utf-8",
    )
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(
        """schema_version: 1
runtimes:
  - runtime_id: example
    revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    upstream_repository: https://example.test/source
    upstream_revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    python_version: 3.12.12
    source_markers: [pyproject.toml]
    import_modules: [example]
    supported_targets: [linux-x86_64]
    lock_paths: {}
""",
        "utf-8",
    )

    with pytest.raises(ModelPackOperationError, match="no reviewed lock path"):
        validate_runtime_releases(runtime, recipes, tmp_path)


def test_declared_lock_must_exist_before_bundle_publication(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        """schema_version: 1
runtimes:
  - runtime_id: example
    display_name: Example
    revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    upstream_repository: https://example.test/source
    upstream_revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    python_version: 3.12.12
    license_id: Apache-2.0
    managed_directory: managed/runtimes/example
    required_markers: [.whoopy-runtime.json]
    bundles: []
    unavailable_reason: Not published.
""",
        "utf-8",
    )
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(
        """schema_version: 1
runtimes:
  - runtime_id: example
    revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    upstream_repository: https://example.test/source
    upstream_revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    python_version: 3.12.12
    source_markers: [pyproject.toml]
    import_modules: [example]
    supported_targets: [linux-x86_64]
    lock_paths:
      linux-x86_64: release-locks/example-linux-x86_64.txt
""",
        "utf-8",
    )

    with pytest.raises(ModelPackOperationError, match="Reviewed example/linux-x86_64 lock"):
        validate_runtime_releases(runtime, recipes, tmp_path)


def test_runtime_recipe_repository_must_match_registry(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        """schema_version: 1
runtimes:
  - runtime_id: example
    display_name: Example
    revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    upstream_repository: https://example.test/reviewed
    upstream_revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    python_version: 3.12.12
    license_id: Apache-2.0
    managed_directory: managed/runtimes/example
    required_markers: [.whoopy-runtime.json]
    bundles: []
    unavailable_reason: Not published.
""",
        "utf-8",
    )
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(
        """schema_version: 1
runtimes:
  - runtime_id: example
    revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    upstream_repository: https://example.test/different
    upstream_revision: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    python_version: 3.12.12
    source_markers: [pyproject.toml]
    import_modules: [example]
    supported_targets: [linux-x86_64]
    lock_paths: {}
""",
        "utf-8",
    )

    with pytest.raises(ModelPackOperationError, match="disagrees with runtime registry"):
        validate_runtime_releases(runtime, recipes, tmp_path)
