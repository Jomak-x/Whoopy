from __future__ import annotations

import pytest

from whoopy.artifacts import TargetPlatform
from whoopy.hardware import inspect_hardware
from whoopy.model_packs.registry import _normalize_architecture
from whoopy.model_packs.runtime import current_runtime_target
from whoopy.release import detect_release_target


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("AMD64", "x86_64"),
        ("x64", "x86_64"),
        ("x86-64", "x86_64"),
        ("aarch64", "arm64"),
        ("arm64e", "arm64"),
    ],
)
def test_architecture_aliases_are_consistent_across_release_boundaries(
    alias: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("whoopy.artifacts.platform.system", lambda: "Linux")
    monkeypatch.setattr("whoopy.artifacts.platform.machine", lambda: alias)
    monkeypatch.setattr("whoopy.model_packs.runtime.platform.system", lambda: "Linux")
    monkeypatch.setattr("whoopy.model_packs.runtime.platform.machine", lambda: alias)
    monkeypatch.setattr("whoopy.hardware._has_working_command", lambda _command: False)

    assert TargetPlatform.current().architecture == expected
    assert inspect_hardware().architecture == expected
    assert current_runtime_target() == f"linux-{expected}"
    assert _normalize_architecture(alias) == expected
    assert (
        detect_release_target(operating_system="Linux", architecture=alias).architecture == expected
    )


@pytest.mark.parametrize("alias", ["Windows", "win32", "WinNT"])
def test_windows_system_aliases_resolve_to_the_x64_release_target(alias: str) -> None:
    target = detect_release_target(operating_system=alias, architecture="AMD64")

    assert target.target_id == "windows-x86_64"
    assert target.supported is True
