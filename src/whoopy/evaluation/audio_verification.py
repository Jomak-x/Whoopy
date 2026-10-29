"""Independent benchmark evidence, not a subjective voice quality score.

PCM checks alone cannot establish speech. A passing report requires a successful
independent ASR transcript aligned to the expected words. ASR is fallible: review
and fail results require listening before attributing an error to the synthesizer.
This module has no model or scientific-Python dependency.
"""

from __future__ import annotations

import hashlib
import math
import struct
import unicodedata
import wave
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any


def normalize_words(text: str) -> list[str]:
    """Ignore case/punctuation; retain contractions as one word."""
    text = unicodedata.normalize("NFKC", text).lower().replace("\u2019", "'")
    cleaned = "".join(c if c.isalnum() or c == "'" else " " for c in text)
    return [word.strip("'") for word in cleaned.split() if word.strip("'")]


def compare_transcripts(expected: str, observed: str) -> dict[str, Any]:
    """Levenshtein alignment with explicit ordered omissions/additions/substitutions."""
    reference, hypothesis = normalize_words(expected), normalize_words(observed)
    if not reference:
        raise ValueError("Expected transcript must contain words.")
    costs = [[0] * (len(hypothesis) + 1) for _ in range(len(reference) + 1)]
    for i in range(len(reference) + 1):
        costs[i][0] = i
    for j in range(len(hypothesis) + 1):
        costs[0][j] = j
    for i, word in enumerate(reference, 1):
        for j, actual in enumerate(hypothesis, 1):
            costs[i][j] = min(
                costs[i - 1][j] + 1,
                costs[i][j - 1] + 1,
                costs[i - 1][j - 1] + (word != actual),
            )
    i, j = len(reference), len(hypothesis)
    alignment: list[dict[str, Any]] = []
    while i or j:
        if i and j and costs[i][j] == costs[i - 1][j - 1] + (reference[i - 1] != hypothesis[j - 1]):
            alignment.append(
                {
                    "operation": "match" if reference[i - 1] == hypothesis[j - 1] else "substitute",
                    "expected": reference[i - 1],
                    "observed": hypothesis[j - 1],
                    "expected_index": i - 1,
                    "observed_index": j - 1,
                }
            )
            i, j = i - 1, j - 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            alignment.append(
                {
                    "operation": "delete",
                    "expected": reference[i - 1],
                    "observed": None,
                    "expected_index": i - 1,
                }
            )
            i -= 1
        else:
            alignment.append(
                {
                    "operation": "insert",
                    "expected": None,
                    "observed": hypothesis[j - 1],
                    "observed_index": j - 1,
                }
            )
            j -= 1
    alignment.reverse()
    counts = Counter(item["operation"] for item in alignment)
    error_spans: list[list[dict[str, Any]]] = []
    for item in alignment:
        if item["operation"] == "match":
            if error_spans and error_spans[-1]:
                error_spans.append([])
        else:
            if not error_spans:
                error_spans.append([])
            error_spans[-1].append(item)
    error_spans = [span for span in error_spans if span]
    missing_tail: list[str] = []
    for item in reversed(alignment):
        if item["operation"] == "insert":
            continue
        if item["operation"] != "delete":
            break
        missing_tail.append(item["expected"])
    missing_tail.reverse()
    expected_counts, observed_counts = Counter(reference), Counter(hypothesis)
    repeated = {
        word: count - expected_counts[word]
        for word, count in observed_counts.items()
        if word in expected_counts and count > expected_counts[word]
    }
    return {
        "expected_word_count": len(reference),
        "observed_word_count": len(hypothesis),
        "word_error_rate": costs[-1][-1] / len(reference),
        "ordered_word_coverage": counts["match"] / len(reference),
        "matches": counts["match"],
        "deletions": counts["delete"],
        "insertions": counts["insert"],
        "substitutions": counts["substitute"],
        "longest_contiguous_error_span": max((len(span) for span in error_spans), default=0),
        "error_spans": error_spans,
        "missing_words": [x["expected"] for x in alignment if x["operation"] == "delete"],
        "unexpected_words": [x["observed"] for x in alignment if x["operation"] == "insert"],
        "excess_repeated_words": repeated,
        "missing_tail_words": missing_tail,
        "possible_truncation": len(missing_tail) >= min(3, len(reference)),
        "alignment": alignment,
    }


def inspect_samples(samples: list[float], sample_rate: int) -> dict[str, Any]:
    """Supplemental PCM diagnostics. Noise can pass every one of these checks."""
    if sample_rate <= 0 or not samples:
        raise ValueError("Audio needs a positive sample rate and nonempty samples.")
    finite = [value for value in samples if math.isfinite(value)]
    peak = max((abs(value) for value in finite), default=0.0)
    rms = math.sqrt(sum(value * value for value in finite) / max(1, len(finite)))
    window_size = max(1, sample_rate // 50)
    windows = [finite[i : i + window_size] for i in range(0, len(finite), window_size)]
    silent = [math.sqrt(sum(x * x for x in part) / len(part)) < 0.001 for part in windows]
    leading = next((i for i, value in enumerate(silent) if not value), len(silent))
    trailing = next((i for i, value in enumerate(reversed(silent)) if not value), len(silent))
    crossings = sum((a >= 0) != (b >= 0) for a, b in pairwise(finite))
    # Difference-energy emphasizes high frequencies; this is not speech recognition.
    difference_energy = sum((b - a) ** 2 for a, b in pairwise(finite))
    signal_energy = sum(value * value for value in finite)
    return {
        "frame_count": len(samples),
        "sample_rate": sample_rate,
        "duration_seconds": len(samples) / sample_rate,
        "nonfinite_samples": len(samples) - len(finite),
        "peak": peak,
        "rms": rms,
        "clipped_fraction": sum(abs(value) >= 0.999 for value in finite) / max(1, len(finite)),
        "silent_window_fraction": sum(silent) / max(1, len(silent)),
        "leading_silence_seconds": leading * window_size / sample_rate,
        "trailing_silence_seconds": trailing * window_size / sample_rate,
        "zero_crossing_fraction": crossings / max(1, len(finite) - 1),
        "difference_to_signal_energy": difference_energy / max(1e-30, signal_energy),
        "speech_established_by_pcm": False,
    }


def read_wave(path: Path) -> tuple[list[float], int, int]:
    """Read interleaved PCM with stdlib, float WAV via optional soundfile."""
    try:
        with wave.open(str(path), "rb") as source:
            width, channels = source.getsampwidth(), source.getnchannels()
            rate, frames = source.getframerate(), source.getnframes()
            raw = source.readframes(frames)
        if len(raw) != frames * channels * width:
            raise ValueError("Truncated WAV payload.")
        if width == 1:
            values = [(value - 128) / 128 for value in raw]
        elif width in (2, 4):
            code = "h" if width == 2 else "i"
            values = [
                value[0] / 2 ** (width * 8 - 1) for value in struct.iter_unpack("<" + code, raw)
            ]
        elif width == 3:
            values = [
                int.from_bytes(raw[i : i + 3], "little", signed=True) / 8388608
                for i in range(0, len(raw), 3)
            ]
        else:
            raise ValueError(f"Unsupported PCM width: {width}.")
        return values, rate, channels
    except wave.Error:
        import soundfile  # type: ignore[import-not-found]

        data, rate = soundfile.read(str(path), dtype="float64", always_2d=True)
        return data.reshape(-1).tolist(), int(rate), int(data.shape[1])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_audio(
    path: Path,
    expected: str,
    *,
    asr_result: dict[str, Any] | None,
    asr_error: str | None = None,
    mode: str = "speech",
) -> dict[str, Any]:
    """Build fail-closed evidence; `narration` allows long intentional silence."""
    if mode not in {"speech", "narration"}:
        raise ValueError("mode must be speech or narration")
    if not normalize_words(expected):
        raise ValueError("Expected transcript must contain words.")
    failures: list[str] = []
    reviews: list[str] = []
    pcm: dict[str, Any] | None = None
    audio_digest = None
    try:
        audio_digest = file_sha256(path)
        samples, rate, channels = read_wave(path)
        # Interleaved samples give correct combined RMS/silence over 20ms windows.
        # Spectral proxies must be measured per channel, never across interleaving.
        pcm = inspect_samples(samples, rate * channels)
        pcm.update(
            {
                "sample_rate": rate,
                "frame_count": len(samples) // channels,
                "channel_count": channels,
            }
        )
        if channels > 1:
            channel_diagnostics = [
                inspect_samples(samples[i::channels], rate) for i in range(channels)
            ]
            pcm["channels"] = channel_diagnostics
            for key in (
                "zero_crossing_fraction",
                "difference_to_signal_energy",
                "clipped_fraction",
            ):
                pcm[key] = max(channel[key] for channel in channel_diagnostics)
        if pcm["nonfinite_samples"]:
            failures.append("Audio contains nonfinite samples.")
        if pcm["peak"] == 0 or pcm["rms"] < 0.00001:
            failures.append("Audio is silent or effectively silent.")
        if pcm["clipped_fraction"] > 0.01:
            failures.append("More than 1% of samples reach digital clipping.")
        elif pcm["clipped_fraction"] > 0.001:
            reviews.append("More than 0.1% of samples reach digital clipping.")
        if mode == "speech" and pcm["silent_window_fraction"] > 0.8:
            reviews.append("Speech-only sample contains over 80% near-silent windows.")
        if pcm["zero_crossing_fraction"] > 0.4:
            reviews.append("High zero-crossing rate: inspect possible broadband noise.")
    except (OSError, ValueError, ImportError, EOFError, RuntimeError) as error:
        failures.append(f"PCM inspection unavailable: {error}")
    comparison = None
    if asr_error or asr_result is None:
        failures.append(f"Independent ASR unavailable: {asr_error or 'no result'}")
    elif not isinstance(asr_result.get("text"), str):
        failures.append("Independent ASR result lacks a transcript.")
    else:
        comparison = compare_transcripts(expected, asr_result["text"])
        if not comparison["observed_word_count"]:
            failures.append("Independent ASR recognized no words.")
        if comparison["word_error_rate"] > 0.35 or comparison["ordered_word_coverage"] < 0.65:
            failures.append("ASR transcript substantially disagrees with expected content.")
        elif comparison["word_error_rate"] > 0.15 or comparison["ordered_word_coverage"] < 0.85:
            reviews.append("ASR transcript needs review for missing, changed, or extra words.")
        if comparison["possible_truncation"]:
            failures.append("Expected ending is missing: possible truncated speech.")
        if comparison["longest_contiguous_error_span"] >= 8:
            failures.append("ASR found a contiguous block of at least eight word-level errors.")
        elif comparison["longest_contiguous_error_span"] >= 3:
            reviews.append("ASR found a contiguous block of at least three word-level errors.")
        if comparison["insertions"] >= max(3, math.ceil(comparison["expected_word_count"] * 0.1)):
            reviews.append("Unexpected ASR words: inspect repetition or hallucinated content.")
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "fail" if failures else "review" if reviews else "pass",
        "failure_reasons": failures,
        "review_reasons": reviews,
        "audio_path": str(path.resolve()),
        "audio_sha256": audio_digest,
        "expected_transcript": expected,
        "expected_text_sha256": hashlib.sha256(expected.encode()).hexdigest(),
        "mode": mode,
        "pcm": pcm,
        "asr": asr_result,
        "content": comparison,
        "limits": [
            "ASR is independent evidence, not a human transcription or a proof of perfect speech.",
            "Noise rejection requires content agreement; duration and RMS do not prove speech.",
            "Warmth, naturalness, voice identity, and quality require listener judgment.",
            "Narration mode permits intentional silence; expected words still require recognition.",
        ],
        "thresholds": {
            "pass_max_wer": 0.15,
            "pass_min_coverage": 0.85,
            "fail_wer_above": 0.35,
            "fail_coverage_below": 0.65,
            "fail_contiguous_error_span_at_least": 8,
            "review_contiguous_error_span_at_least": 3,
        },
    }
