from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest

from scripts.raw_voice_bakeoff import assemble, parse_script


def _wav(path: Path, frames: bytes, *, rate: int = 24_000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(frames)


def test_parse_preserves_prose_and_only_compiles_authored_pauses() -> None:
    events = parse_script(
        "# Title\n\nFirst sentence.\n\nSecond sentence.\n\n"
        "**[Pause for 2.5 seconds]**\n\nFinal words.\n\n"
        "**[Final silence for 60 seconds]**\n"
    )
    assert [(event.kind, event.text, event.seconds) for event in events] == [
        ("speech", "First sentence.\n\nSecond sentence.", None),
        ("silence", None, 2.5),
        ("speech", "Final words.", None),
        ("silence", None, 60.0),
    ]


def test_assembly_copies_speech_pcm_exactly_and_adds_only_zero_silence(tmp_path: Path) -> None:
    speech = tmp_path / "speech"
    speech.mkdir()
    first = b"\x01\x00\x02\x00"
    second = b"\x03\x00\x04\x00"
    _wav(speech / "speech-001.wav", first, rate=4)
    _wav(speech / "speech-002.wav", second, rate=4)
    events = parse_script("One.\n\n**[Pause for 1 seconds]**\n\nTwo.")
    output = tmp_path / "raw.wav"

    manifest = assemble(events=events, speech_directory=speech, output=output)

    with wave.open(str(output), "rb") as assembled:
        frames = assembled.readframes(assembled.getnframes())
    assert frames == first + bytes(4 * 2) + second
    assert manifest["processing"] == (
        "none; native PCM copied byte-for-byte; authored silence only"
    )
    assert manifest["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_assembly_refuses_existing_audio_or_receipt_without_changes(tmp_path: Path) -> None:
    speech = tmp_path / "speech"
    speech.mkdir()
    _wav(speech / "speech-001.wav", b"\x01\x00", rate=4)
    events = parse_script("One.")
    for existing_name in ("raw.wav", "raw.manifest.json"):
        folder = tmp_path / existing_name.replace(".", "-")
        folder.mkdir()
        (folder / existing_name).write_bytes(b"original evidence")
        before = {path.name: path.read_bytes() for path in folder.iterdir()}

        with pytest.raises(FileExistsError, match="fresh output path"):
            assemble(events=events, speech_directory=speech, output=folder / "raw.wav")

        assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
