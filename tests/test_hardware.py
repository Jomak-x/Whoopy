from __future__ import annotations

from pathlib import Path

from serenity.hardware import HardwareSnapshot, diagnose, load_runtime_profiles


def _snapshot(*, total: float, available: float, disk: float) -> HardwareSnapshot:
    return HardwareSnapshot(
        operating_system="linux",
        architecture="x86_64",
        cpu_count=4,
        total_ram_gb=total,
        available_ram_gb=available,
        free_disk_gb=disk,
        accelerators=["cpu"],
    )


def test_selects_highest_profile_with_safe_live_resources() -> None:
    profiles = load_runtime_profiles(Path("config/runtime_profiles.yaml"))

    result = diagnose(_snapshot(total=16, available=9, disk=20), profiles)

    assert result.supported is True
    assert result.selected_profile is not None
    assert result.selected_profile.name == "standard"
    assert result.selected_profile.llm_runtime == "llama_cpp"


def test_falls_back_to_basic_instead_of_loading_an_unsafe_llm() -> None:
    profiles = load_runtime_profiles(Path("config/runtime_profiles.yaml"))

    result = diagnose(_snapshot(total=8, available=2, disk=10), profiles)

    assert result.supported is True
    assert result.selected_profile is not None
    assert result.selected_profile.name == "basic"
    assert result.selected_profile.llm_runtime == "none"
    assert "templates" in result.selected_profile.modes
    assert "local_tts" in result.selected_profile.modes


def test_refuses_to_load_when_basic_safety_margin_does_not_fit() -> None:
    profiles = load_runtime_profiles(Path("config/runtime_profiles.yaml"))

    result = diagnose(_snapshot(total=2, available=1, disk=10), profiles)

    assert result.supported is False
    assert result.selected_profile is None
    assert "will not attempt a model load" in result.messages[-1]


def test_refuses_an_untested_operating_system() -> None:
    profiles = load_runtime_profiles(Path("config/runtime_profiles.yaml"))
    snapshot = _snapshot(total=64, available=48, disk=100)
    snapshot = snapshot.model_copy(update={"operating_system": "unknown-os"})

    result = diagnose(snapshot, profiles)

    assert result.supported is False
    assert result.selected_profile is None
    assert "No native release target" in result.messages[0]


def test_named_profile_can_be_checked_without_selecting_a_larger_one() -> None:
    profiles = load_runtime_profiles(Path("config/runtime_profiles.yaml"))

    result = diagnose(
        _snapshot(total=48, available=32, disk=100),
        profiles,
        requested_profile="lite",
    )

    assert result.supported is True
    assert result.selected_profile is not None
    assert result.selected_profile.name == "lite"
