"""Executable MOSS browser/API contract after the Studio redesign.

The retired model-settings UI used source-text assertions for controls that no
longer exist. Keep the underlying typed controls and Studio/Lab boundary covered
here; DOM interaction and keyboard behavior require a real browser suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from whoopy.webui.api import GenerationRequest


@pytest.mark.parametrize("model", ["moss-local-v1.5", "moss-v1.5"])
@pytest.mark.parametrize("reference", [None, "consented-narrator"])
def test_moss_studio_accepts_reviewed_instruction_and_typed_voice_source(
    model: str, reference: str | None
) -> None:
    payload: dict[str, Any] = {
        "mode": "script",
        "text": "Let your shoulders soften.",
        "tts_model": model,
        "moss_language": "English",
        "moss_instruction": "Speak slowly, softly, and warmly, with a meditative delivery.",
        "moss_use_reference": reference is not None,
    }
    if reference is not None:
        payload["voice_reference"] = reference

    request = GenerationRequest.model_validate(payload)

    assert request.surface == "studio"
    serialized = request.model_dump(exclude_none=True)
    assert {key: serialized[key] for key in payload} == payload
    assert request.voice_reference == reference


@pytest.mark.parametrize("model", ["moss-local-v1.5", "moss-v1.5"])
def test_arbitrary_moss_instruction_is_confined_to_lab(model: str) -> None:
    payload = {
        "mode": "script",
        "text": "Rest here.",
        "tts_model": model,
        "moss_instruction": "An experimental delivery for controlled comparison.",
    }
    with pytest.raises(ValidationError, match="Free-form MOSS instructions are Lab-only"):
        GenerationRequest.model_validate(payload)

    request = GenerationRequest.model_validate({**payload, "surface": "lab"})
    assert request.moss_instruction == payload["moss_instruction"]


@pytest.mark.parametrize("model", ["moss-local-v1.5", "moss-v1.5"])
def test_moss_direct_voice_rejects_a_reference_instead_of_ignoring_it(model: str) -> None:
    with pytest.raises(ValidationError, match="direct voice mode cannot use a voice reference"):
        GenerationRequest.model_validate(
            {
                "mode": "script",
                "text": "Rest here.",
                "tts_model": model,
                "moss_use_reference": False,
                "voice_reference": "consented-narrator",
            }
        )
