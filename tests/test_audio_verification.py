from __future__ import annotations

import json
import math
import random
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from whoopy.evaluation.audio_verification import (
    compare_transcripts,
    inspect_samples,
    normalize_words,
    verify_audio,
)


def save_wave(path: Path, samples: list[float]) -> None:
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", int(value * 32767)) for value in samples))


@pytest.fixture
def audible_wave(tmp_path: Path) -> Path:
    path = tmp_path / "tone.wav"
    save_wave(path, [0.2 * math.sin(i / 10) for i in range(16000)])
    return path


def test_words_ignore_punctuation_and_case_without_discarding_contractions() -> None:
    assert normalize_words("Don't rush. You\u2019re safe.") == ["don't", "rush", "you're", "safe"]
    result = compare_transcripts("Breathe in, slowly.", "breathe IN slowly")
    assert result["word_error_rate"] == 0
    assert result["ordered_word_coverage"] == 1


def test_alignment_records_omissions_repeats_and_added_content() -> None:
    result = compare_transcripts("one two three four five", "one two two four five subscribe")
    assert result["word_error_rate"] > 0
    assert result["ordered_word_coverage"] == 0.8
    assert result["excess_repeated_words"] == {"two": 1}
    assert "subscribe" in result["unexpected_words"]
    assert sum(result[key] for key in ("deletions", "insertions", "substitutions")) >= 2


def test_truncated_ending_is_explicit() -> None:
    result = compare_transcripts("one two three four five six seven", "one two three four")
    assert result["missing_tail_words"] == ["five", "six", "seven"]
    assert result["possible_truncation"] is True


def test_order_matters_even_when_word_bags_are_equal() -> None:
    result = compare_transcripts("one two three four five", "five four three two one")
    assert result["ordered_word_coverage"] < 0.65


def test_asr_unavailable_fails_closed_despite_valid_audible_pcm(audible_wave: Path) -> None:
    report = verify_audio(audible_wave, "Breathe slowly", asr_result=None)
    assert report["status"] == "fail"
    assert report["pcm"]["rms"] > 0.1
    assert any("ASR unavailable" in reason for reason in report["failure_reasons"])


def test_empty_asr_rejects_noise_with_healthy_rms(tmp_path: Path) -> None:
    # This tests decision behavior with an empty recognizer result, not ASR accuracy.
    # Synthetic noise has plausible duration/RMS: those must not establish speech.
    generator = random.Random(9)
    noise = [generator.uniform(-0.2, 0.2) for _ in range(16000)]
    path = tmp_path / "noise.wav"
    save_wave(path, noise)
    report = verify_audio(
        path, "Breathe in slowly and let your shoulders soften", asr_result={"text": ""}
    )
    assert report["pcm"]["duration_seconds"] == 1
    assert report["pcm"]["rms"] > 0.1
    assert report["pcm"]["speech_established_by_pcm"] is False
    assert report["status"] == "fail"
    assert report["content"]["ordered_word_coverage"] == 0
    assert any("recognized no words" in reason for reason in report["failure_reasons"])


def test_unrelated_asr_fails_even_with_matching_word_count(audible_wave: Path) -> None:
    report = verify_audio(
        audible_wave, "Breathe in slowly", asr_result={"text": "Subscribe to channel"}
    )
    assert report["status"] == "fail"
    assert report["content"]["word_error_rate"] == 1


def test_expected_content_passes_but_does_not_claim_listener_quality(audible_wave: Path) -> None:
    report = verify_audio(audible_wave, "Breathe slowly.", asr_result={"text": "Breathe slowly"})
    assert report["status"] == "pass"
    assert any("listener judgment" in limit for limit in report["limits"])
    assert len(report["audio_sha256"]) == 64


def test_moderate_content_error_needs_review(audible_wave: Path) -> None:
    report = verify_audio(
        audible_wave,
        "one two three four five six seven eight nine ten",
        asr_result={"text": "one two three RED BLUE six seven eight nine ten"},
    )
    assert report["status"] == "review"
    assert report["content"]["word_error_rate"] == 0.2


def test_localized_bad_block_cannot_hide_in_low_whole_script_error(audible_wave: Path) -> None:
    words = [f"word{index}" for index in range(100)]
    observed = words[:40] + [f"gibberish{index}" for index in range(8)] + words[48:]
    report = verify_audio(audible_wave, " ".join(words), asr_result={"text": " ".join(observed)})
    assert report["content"]["word_error_rate"] == 0.08
    assert report["content"]["ordered_word_coverage"] == 0.92
    assert report["content"]["longest_contiguous_error_span"] == 8
    assert report["status"] == "fail"
    assert any("contiguous block" in reason for reason in report["failure_reasons"])


def test_silence_and_clipping_fail_even_when_asr_claims_correct_text(tmp_path: Path) -> None:
    for name, values in (("silence", [0.0] * 16000), ("clipping", [1.0] * 16000)):
        path = tmp_path / f"{name}.wav"
        save_wave(path, values)
        report = verify_audio(path, "breathe", asr_result={"text": "breathe"})
        assert report["status"] == "fail"


def test_nonfinite_diagnostics_do_not_hide_invalid_float_samples() -> None:
    result = inspect_samples([0.2, math.nan, math.inf, -0.2], 16000)
    assert result["nonfinite_samples"] == 2
    assert math.isfinite(result["rms"])


def test_stereo_inspects_all_channels_without_interleaving_spectral_artifacts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stereo.wav"
    values = [(0.2 * math.sin(i / 10), 0.1 * math.sin(i / 10)) for i in range(16000)]
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(
            b"".join(struct.pack("<hh", int(a * 32767), int(b * 32767)) for a, b in values)
        )
    report = verify_audio(path, "breathe", asr_result={"text": "breathe"})
    assert report["status"] == "pass"
    assert report["pcm"]["duration_seconds"] == 1
    assert report["pcm"]["frame_count"] == 16000
    assert report["pcm"]["channel_count"] == 2
    assert len(report["pcm"]["channels"]) == 2
    assert report["pcm"]["zero_crossing_fraction"] < 0.1


def test_narration_intentional_silence_is_not_a_content_failure(tmp_path: Path) -> None:
    path = tmp_path / "paced.wav"
    save_wave(path, [0.0] * 15000 + [0.2 * math.sin(i / 10) for i in range(1000)])
    report = verify_audio(path, "breathe", asr_result={"text": "breathe"}, mode="narration")
    assert report["status"] == "pass"
    speech_report = verify_audio(path, "breathe", asr_result={"text": "breathe"}, mode="speech")
    assert speech_report["status"] == "review"


def test_invalid_or_missing_audio_and_empty_reference_cannot_pass(tmp_path: Path) -> None:
    report = verify_audio(tmp_path / "missing.wav", "breathe", asr_result={"text": "breathe"})
    assert report["status"] == "fail"
    with pytest.raises(ValueError, match="Expected transcript"):
        compare_transcripts("...", "breathe")


def test_cli_writes_structured_failure_when_asr_runtime_missing(audible_wave: Path) -> None:
    report_path = audible_wave.parent / "report.json"
    script = Path(__file__).resolve().parents[1] / "scripts/verify_benchmark_audio.py"
    process = subprocess.run(
        [
            sys.executable,
            str(script),
            str(audible_wave),
            "--expected-text",
            "Breathe slowly",
            "--report",
            str(report_path),
            "--asr-python",
            str(audible_wave.parent / "missing-python"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 1
    report = json.loads(report_path.read_text())
    assert report["status"] == "fail"
    assert report["asr"] is None
    assert report["content"] is None
    assert report["pcm"]["rms"] > 0.1
