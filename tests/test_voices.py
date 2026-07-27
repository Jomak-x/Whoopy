from __future__ import annotations

import pytest

from whoopy.voices import KOKORO_ENGLISH_VOICES, kokoro_speaker_id


def test_reviewed_voice_names_map_to_unique_pinned_bundle_ids() -> None:
    assert kokoro_speaker_id("af_heart") == 3
    assert kokoro_speaker_id("af_bella") == 2
    assert len(KOKORO_ENGLISH_VOICES.values()) == len(set(KOKORO_ENGLISH_VOICES.values()))


def test_unknown_voice_is_rejected_with_available_choices() -> None:
    with pytest.raises(ValueError, match="af_heart"):
        kokoro_speaker_id("unknown")
