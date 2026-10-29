from __future__ import annotations

import hashlib
import math
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from whoopy.voice_references import (
    VoiceConsent,
    VoiceReferenceError,
    VoiceReferenceManager,
)


def _consent() -> VoiceConsent:
    return VoiceConsent(
        confirmed=True,
        scope="local_voice_cloning_experiment_only",
        source_kind="user_provided",
        rights_attestation="I own this voice or have permission to clone it.",
        recorded_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


def _wav(path: Path, *, rate: int = 48_000, seconds: float = 3, channels: int = 2) -> None:
    frames = round(rate * seconds)
    samples = array("h")
    for index in range(frames):
        value = round(6_000 * math.sin(2 * math.pi * 220 * index / rate))
        samples.append(value)
        if channels == 2:
            samples.append(value // 2)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())


def test_import_canonicalizes_and_records_complete_preview_evidence(tmp_path: Path) -> None:
    source = tmp_path / "input.wav"
    _wav(source)
    manager = VoiceReferenceManager(tmp_path / "managed")

    record = manager.import_wav(
        reference_id="my-voice",
        display_name="  My   Voice  ",
        source_wav=source,
        transcript="  A calm and accurately transcribed reference.  ",
        consent=_consent(),
        trim_start_ms=500,
        trim_end_ms=2_500,
    )

    assert record.display_name == "My Voice"
    assert record.revision == 1
    assert record.duration_ms == 2_000
    assert record.sample_rate == 24_000
    assert record.channels == 1
    assert record.source_sample_rate == 48_000
    assert record.source_channels == 2
    assert record.trim_start_ms == 500
    assert record.trim_end_ms == 2_500
    assert record.consent == _consent()
    assert record.preview.duration_ms == 2_000
    audio = manager.root / record.audio_path
    transcript = manager.root / record.transcript_path
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == record.audio_sha256
    assert hashlib.sha256(transcript.read_bytes()).hexdigest() == record.transcript_sha256
    with wave.open(str(audio), "rb") as reader:
        assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (
            24_000,
            1,
            2,
        )
        assert reader.getnframes() == 48_000
    assert manager.list()[0].revision_id == record.revision_id


def test_replace_creates_new_immutable_revision_and_preserves_original(tmp_path: Path) -> None:
    first_source = tmp_path / "first.wav"
    second_source = tmp_path / "second.wav"
    _wav(first_source, rate=24_000, seconds=2)
    _wav(second_source, rate=24_000, seconds=4)
    manager = VoiceReferenceManager(tmp_path / "managed")
    first = manager.import_wav(
        reference_id="narrator",
        display_name="Narrator",
        source_wav=first_source,
        transcript="First transcript.",
        consent=_consent(),
    )
    first_audio = manager.root / first.audio_path
    original_payload = first_audio.read_bytes()

    second = manager.replace(
        reference_id="narrator",
        display_name="Narrator revised",
        source_wav=second_source,
        transcript="Replacement transcript.",
        consent=_consent(),
        trim_start_ms=1_000,
        trim_end_ms=3_000,
    )

    assert second.revision == 2
    assert second.parent_revision_id == first.revision_id
    assert manager.load("narrator") == second
    assert first_audio.read_bytes() == original_payload
    assert (first_audio.parent / "manifest.json").is_file()


def test_consent_must_be_explicit_and_exact() -> None:
    with pytest.raises(ValidationError):
        VoiceConsent.model_validate(
            {
                "confirmed": False,
                "scope": "local_voice_cloning_experiment_only",
                "source_kind": "user_provided",
                "rights_attestation": "I own this voice or have permission to clone it.",
                "recorded_at": datetime.now(UTC),
            }
        )


def test_rejects_path_traversal_and_symlink_sources(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _wav(source)
    manager = VoiceReferenceManager(tmp_path / "managed")
    with pytest.raises(VoiceReferenceError, match="invalid voice reference ID"):
        manager.import_wav(
            reference_id="../outside",
            display_name="Unsafe",
            source_wav=source,
            transcript="Transcript.",
            consent=_consent(),
        )

    linked = tmp_path / "linked.wav"
    linked.symlink_to(source)
    with pytest.raises(VoiceReferenceError, match="may not be a symlink"):
        manager.import_wav(
            reference_id="linked",
            display_name="Linked",
            source_wav=linked,
            transcript="Transcript.",
            consent=_consent(),
        )


def test_list_rejects_symlinked_managed_entries(tmp_path: Path) -> None:
    manager = VoiceReferenceManager(tmp_path / "managed")
    outside = tmp_path / "outside"
    outside.mkdir()
    references = manager.root / "references"
    references.mkdir(parents=True)
    (references / "unsafe").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VoiceReferenceError, match="unsafe entry"):
        manager.list()


def test_load_detects_audio_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _wav(source, rate=24_000, seconds=2, channels=1)
    manager = VoiceReferenceManager(tmp_path / "managed")
    record = manager.import_wav(
        reference_id="tamper-test",
        display_name="Tamper test",
        source_wav=source,
        transcript="Transcript.",
        consent=_consent(),
    )
    (manager.root / record.audio_path).write_bytes(b"changed")

    with pytest.raises(VoiceReferenceError, match="audio hash does not match"):
        manager.load("tamper-test")


def test_load_rejects_symlink_substitution_inside_revision(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _wav(source, rate=24_000, seconds=2, channels=1)
    manager = VoiceReferenceManager(tmp_path / "managed")
    record = manager.import_wav(
        reference_id="link-substitution",
        display_name="Link substitution",
        source_wav=source,
        transcript="Transcript.",
        consent=_consent(),
    )
    stored_audio = manager.root / record.audio_path
    stored_audio.unlink()
    stored_audio.symlink_to(source)

    with pytest.raises(VoiceReferenceError, match="symlink"):
        manager.load("link-substitution")


def test_deletion_requires_confirmation_and_is_recoverable(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    _wav(source, rate=24_000, seconds=2, channels=1)
    manager = VoiceReferenceManager(tmp_path / "managed")
    original = manager.import_wav(
        reference_id="recoverable",
        display_name="Recoverable",
        source_wav=source,
        transcript="Transcript.",
        consent=_consent(),
    )

    with pytest.raises(VoiceReferenceError, match="confirmation"):
        manager.delete("recoverable", confirmation="delete")
    receipt = manager.delete(
        "recoverable",
        confirmation=manager.delete_confirmation("recoverable"),
    )
    assert manager.list() == ()
    assert (manager.root / receipt.payload_path).is_dir()

    with pytest.raises(VoiceReferenceError, match="confirmation"):
        manager.restore(receipt.trash_id, confirmation="restore")
    restored = manager.restore(
        receipt.trash_id,
        confirmation=manager.restore_confirmation(receipt.trash_id),
    )
    assert restored == original
    assert manager.list()[0].reference_id == "recoverable"


def test_rejects_silent_or_out_of_bounds_trimmed_audio(tmp_path: Path) -> None:
    silent = tmp_path / "silent.wav"
    with wave.open(str(silent), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(bytes(24_000 * 2))
    manager = VoiceReferenceManager(tmp_path / "managed")

    with pytest.raises(VoiceReferenceError, match="no audible signal"):
        manager.import_wav(
            reference_id="silent",
            display_name="Silent",
            source_wav=silent,
            transcript="Transcript.",
            consent=_consent(),
        )
    source = tmp_path / "source.wav"
    _wav(source, rate=24_000, seconds=2, channels=1)
    with pytest.raises(VoiceReferenceError, match="exceeds"):
        manager.import_wav(
            reference_id="bad-trim",
            display_name="Bad trim",
            source_wav=source,
            transcript="Transcript.",
            consent=_consent(),
            trim_end_ms=3_000,
        )
