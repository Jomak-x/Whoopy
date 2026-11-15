from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from whoopy.evaluation.audio_verification import verify_audio


@pytest.fixture
def audible_wave(tmp_path: Path) -> Path:
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(
            b"".join(struct.pack("<h", int(6000 * math.sin(i / 10))) for i in range(16000))
        )
    return path


def test_optional_metric_warning_requires_review_even_with_exact_words(audible_wave: Path) -> None:
    asr = {
        "text": "Breathe slowly",
        "segments": [{"avg_logprob": None}],
        "metric_warnings": [".segments[0].avg_logprob"],
    }
    report = verify_audio(audible_wave, "Breathe slowly", asr_result=asr)
    assert report["status"] == "review"
    assert report["content"]["word_error_rate"] == 0
    assert report["content"]["ordered_word_coverage"] == 1
    assert report["asr"] == asr
    assert not report["failure_reasons"]
    assert any(".segments[0].avg_logprob" in reason for reason in report["review_reasons"])


def test_finite_optional_metrics_allow_existing_exact_word_pass(audible_wave: Path) -> None:
    report = verify_audio(
        audible_wave,
        "Breathe slowly",
        asr_result={"text": "Breathe slowly", "metric_warnings": []},
    )
    assert report["status"] == "pass"
    assert not report["review_reasons"]


@pytest.mark.parametrize("observed", ["Breathe very slowly", "unrelated words", ""])
def test_metric_warnings_preserve_word_policy_and_failures(
    audible_wave: Path, observed: str
) -> None:
    baseline = verify_audio(audible_wave, "Breathe slowly", asr_result={"text": observed})
    warned = verify_audio(
        audible_wave,
        "Breathe slowly",
        asr_result={"text": observed, "metric_warnings": [".segments[0].avg_logprob"]},
    )
    assert warned["status"] == baseline["status"]
    assert warned["content"] == baseline["content"]
    assert warned["failure_reasons"] == baseline["failure_reasons"]
    assert warned["thresholds"] == baseline["thresholds"]
    assert warned["policy_id"] == "normalized-word-mismatch-review-v3"


def test_unavailable_asr_still_fails(audible_wave: Path) -> None:
    report = verify_audio(audible_wave, "Breathe slowly", asr_result=None)
    assert report["status"] == "fail"
    assert report["content"] is None
