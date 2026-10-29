"""Plan and losslessly assemble raw native-WAV voice bakeoffs.

This deliberately does not trim, normalize, fade, resample, time-stretch, or
otherwise process model speech.  It copies each PCM speech WAV byte-for-byte
into the final container and inserts only the silences explicitly authored in
the source meditation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import wave
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

PAUSE = re.compile(
    r"^\*{0,2}\[(?:pause for|final silence for)\s+"
    r"(?P<seconds>\d+(?:\.\d+)?)\s+seconds\]\*{0,2}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Event:
    kind: str
    text: str | None = None
    seconds: float | None = None


def parse_script(source: str) -> list[Event]:
    """Preserve authored prose and convert only explicit pause lines."""

    events: list[Event] = []
    speech_paragraphs: list[str] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_lines:
            speech_paragraphs.append(" ".join(paragraph_lines))
            paragraph_lines.clear()

    def flush_speech() -> None:
        flush_paragraph()
        if speech_paragraphs:
            events.append(Event(kind="speech", text="\n\n".join(speech_paragraphs)))
            speech_paragraphs.clear()

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if line.startswith("#"):
            flush_paragraph()
            continue
        pause = PAUSE.fullmatch(line)
        if pause:
            flush_speech()
            events.append(Event(kind="silence", seconds=float(pause.group("seconds"))))
            continue
        paragraph_lines.append(line)
    flush_speech()
    return events


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assemble(*, events: list[Event], speech_directory: Path, output: Path) -> dict[str, object]:
    """Copy PCM frames exactly and insert authored zero-valued silence."""

    speech_paths = sorted(speech_directory.glob("speech-*.wav"))
    expected = sum(event.kind == "speech" for event in events)
    if len(speech_paths) != expected:
        raise ValueError(f"expected {expected} speech WAVs, found {len(speech_paths)}")

    manifest_path = output.with_suffix(".manifest.json")
    if output.exists() or manifest_path.exists():
        raise FileExistsError("Evidence already exists; use a fresh output path.")

    format_signature: tuple[int, int, int, str, str] | None = None
    output.parent.mkdir(parents=True, exist_ok=True)
    speech_index = 0
    total_frames = 0
    source_records: list[dict[str, object]] = []
    destination: wave.Wave_write | None = None
    with ExitStack() as stack:
        manifest_file = stack.enter_context(manifest_path.open("x", encoding="utf-8"))
        output_file = stack.enter_context(output.open("xb"))
        for event in events:
            if event.kind == "speech":
                source_path = speech_paths[speech_index]
                speech_index += 1
                with wave.open(str(source_path), "rb") as source:
                    signature = (
                        source.getnchannels(),
                        source.getsampwidth(),
                        source.getframerate(),
                        source.getcomptype(),
                        source.getcompname(),
                    )
                    if source.getcomptype() != "NONE" or source.getsampwidth() not in (2, 3, 4):
                        raise ValueError(f"{source_path} must be uncompressed 16/24/32-bit PCM WAV")
                    if format_signature is None:
                        format_signature = signature
                        destination = stack.enter_context(wave.open(output_file, "wb"))
                        destination.setnchannels(signature[0])
                        destination.setsampwidth(signature[1])
                        destination.setframerate(signature[2])
                        destination.setcomptype(signature[3], signature[4])
                    elif signature != format_signature:
                        raise ValueError(
                            f"native format changed at {source_path}: "
                            f"{signature} != {format_signature}"
                        )
                    frame_count = source.getnframes()
                    frames = source.readframes(frame_count)
                    assert destination is not None
                    destination.writeframesraw(frames)
                    total_frames += frame_count
                    source_records.append(
                        {
                            "path": str(source_path.resolve()),
                            "frames": frame_count,
                            "sha256": _sha256(source_path),
                        }
                    )
            else:
                if destination is None or format_signature is None:
                    raise ValueError(
                        "meditation cannot begin with silence before the native format is known"
                    )
                assert event.seconds is not None
                silence_frames = round(event.seconds * format_signature[2])
                destination.writeframesraw(
                    bytes(silence_frames * format_signature[0] * format_signature[1])
                )
                total_frames += silence_frames
        if destination is None or format_signature is None:
            raise ValueError("meditation contains no speech")

        destination.close()
        output_file.flush()

        manifest: dict[str, object] = {
            "schema_version": 1,
            "processing": "none; native PCM copied byte-for-byte; authored silence only",
            "sample_rate": format_signature[2],
            "channels": format_signature[0],
            "sample_width_bytes": format_signature[1],
            "frames": total_frames,
            "duration_seconds": total_frames / format_signature[2],
            "speech_sources": source_records,
            "output": str(output.resolve()),
            "output_sha256": _sha256(output),
        }
        manifest_file.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--script", type=Path, required=True)
    assemble_parser = subparsers.add_parser("assemble")
    assemble_parser.add_argument("--script", type=Path, required=True)
    assemble_parser.add_argument("--speech-directory", type=Path, required=True)
    assemble_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    events = parse_script(args.script.read_text(encoding="utf-8"))
    if args.command == "plan":
        print(json.dumps([asdict(event) for event in events], indent=2))
        return 0
    result = assemble(
        events=events,
        speech_directory=args.speech_directory,
        output=args.output,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
