"""Voice comparison API contracts retained after the /voices/ UI was retired.

The old A/B page, its localStorage snapshot, and its install buttons are no
longer product features. These tests execute the shared generation contract
used by voice experiments; they do not claim browser comparison coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from whoopy.webui.api import GenerationRequest, create_api
from whoopy.webui.server import LocalWebApplication


@pytest.mark.parametrize(
    ("model", "controls"),
    [
        ("qwen3-custom-voice-0.6b", {"qwen_speaker": "Ryan"}),
        ("qwen3-custom-voice-1.7b", {"qwen_speaker": "Ryan"}),
        ("qwen3-voice-design-1.7b", {"qwen_voice_design_preset": "sleep_meditation"}),
        ("qwen3-base-clone-0.6b", {"qwen_reference": "consented-narrator"}),
        ("qwen3-base-clone-1.7b", {"qwen_reference": "consented-narrator"}),
    ],
)
def test_qwen_variants_preserve_the_same_authored_words_and_typed_controls(
    model: str, controls: dict[str, str]
) -> None:
    text = "Rest your attention here.\n\nThere is nothing to solve."
    request = GenerationRequest.model_validate(
        {
            "surface": "lab",
            "profile": "basic",
            "mode": "script",
            "text": text,
            "tts_model": model,
            "delivery": "neutral",
            "intensity": "subtle",
            "pace": "natural",
            **controls,
        }
    )

    serialized = request.model_dump(exclude_none=True)
    assert serialized["text"] == text
    assert serialized["tts_model"] == model
    for key, value in controls.items():
        assert serialized[key] == value


@pytest.mark.parametrize("preset", ["warm_meditation", "grounded_meditation", "sleep_meditation"])
def test_voice_design_accepts_reviewed_presets(preset: str) -> None:
    request = GenerationRequest.model_validate(
        {
            "mode": "script",
            "text": "Rest here.",
            "tts_model": "qwen3-voice-design-1.7b",
            "qwen_voice_design_preset": preset,
        }
    )
    assert request.qwen_voice_design_preset == preset


@pytest.mark.parametrize("preset", ["sleep_narrator", "grounded_guide", "invented-preset"])
def test_voice_design_rejects_unknown_presets(preset: str) -> None:
    with pytest.raises(ValidationError, match="qwen_voice_design_preset"):
        GenerationRequest.model_validate(
            {
                "mode": "script",
                "text": "Rest here.",
                "tts_model": "qwen3-voice-design-1.7b",
                "qwen_voice_design_preset": preset,
            }
        )


@pytest.mark.parametrize("model", ["qwen3-base-clone-0.6b", "qwen3-base-clone-1.7b"])
def test_reference_clone_requires_a_managed_reference_choice(model: str) -> None:
    with pytest.raises(ValidationError, match="requires a consented voice reference"):
        GenerationRequest.model_validate(
            {
                "mode": "script",
                "text": "Rest here.",
                "tts_model": model,
                "delivery": "neutral",
                "pace": "natural",
            }
        )


@pytest.mark.parametrize(
    "unsupported",
    [{"delivery": "calm"}, {"intensity": "moderate"}, {"pace": "slow"}],
)
def test_basic_qwen_voice_fails_closed_without_capability_fallback(
    unsupported: dict[str, str], tmp_path: Path
) -> None:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    payload: dict[str, Any] = {
        "surface": "lab",
        "mode": "script",
        "text": "Rest here.",
        "tts_model": "qwen3-custom-voice-0.6b",
        "qwen_speaker": "Ryan",
        "delivery": "neutral",
        "intensity": "subtle",
        "pace": "natural",
        **unsupported,
    }
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/generate", json=payload)
        runs = client.get("/api/v1/runs")

    assert response.status_code == 422
    assert "supports only neutral delivery" in response.text
    assert runs.status_code == 200
    assert runs.json()["runs"] == []
