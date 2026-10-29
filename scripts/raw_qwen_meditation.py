"""Official Qwen3-TTS raw-output helper for the controlled voice bakeoff."""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts.raw_voice_bakeoff import parse_script
else:
    from raw_voice_bakeoff import parse_script


REFERENCE_TEXT = (
    "Welcome. Let the weight of the day settle away now. "
    "There is nowhere else you need to be, and nothing you need to solve."
)
DEEP_MALE_DESIGN = (
    "An adult male sleep meditation narrator in his early forties, with a deep warm "
    "baritone, relaxed breath support, gentle low energy, clear intimate diction, "
    "restrained emotion, and a natural unhurried conversational cadence. Keep the pitch "
    "low and stable without whispering, theatricality, vocal fry, or exaggerated pauses."
)
CUSTOM_INSTRUCTION = (
    "Speak as a calm sleep meditation narrator with low energy, restrained warmth, and "
    "a natural unhurried cadence. Do not whisper or exaggerate pauses."
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("design-reference", "custom", "clone"), required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--reference-text", default=REFERENCE_TEXT)
    parser.add_argument("--speaker", default="Ryan")
    parser.add_argument("--device", choices=("mps", "cpu", "cuda"), default="mps")
    parser.add_argument("--seed", type=int, default=7400)
    return parser.parse_args()


def _runtime(torch: object, device: str) -> tuple[str, object, str]:
    if device == "cuda":
        if not torch.cuda.is_available():  # type: ignore[attr-defined]
            raise RuntimeError("CUDA was requested but is unavailable")
        return "cuda:0", torch.bfloat16, "flash_attention_2"  # type: ignore[attr-defined]
    if device == "mps":
        if not torch.backends.mps.is_available():  # type: ignore[attr-defined]
            raise RuntimeError("MPS was requested but is unavailable")
        # Prefer fidelity over throughput for this bakeoff.  No fp16 conversion.
        return "mps", torch.float32, "eager"  # type: ignore[attr-defined]
    return "cpu", torch.float32, "eager"  # type: ignore[attr-defined]


def _write_manifest(directory: Path, payload: dict[str, object]) -> None:
    (directory / "generation.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    args = _arguments()
    try:
        args.output_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise SystemExit("Evidence already exists; use a new output directory.") from error

    import soundfile as sf  # type: ignore[import-not-found]
    import torch  # type: ignore[import-not-found]
    from qwen_tts import Qwen3TTSModel  # type: ignore[import-not-found]

    device, dtype, attention = _runtime(torch, args.device)
    model = Qwen3TTSModel.from_pretrained(
        str(args.model.resolve()),
        device_map=device,
        dtype=dtype,
        attn_implementation=attention,
        local_files_only=True,
    )
    common = {
        "model": str(args.model.resolve()),
        "device": device,
        "dtype": str(dtype),
        "attention": attention,
        "processing": "none",
        "seed": args.seed,
    }

    if args.mode == "design-reference":
        torch.manual_seed(args.seed)
        wavs, rate = model.generate_voice_design(
            text=REFERENCE_TEXT,
            language="English",
            instruct=DEEP_MALE_DESIGN,
            max_new_tokens=2048,
        )
        output = args.output_directory / "deep-male-reference.wav"
        sf.write(output, wavs[0], rate, subtype="PCM_16")
        (args.output_directory / "deep-male-reference.txt").write_text(
            REFERENCE_TEXT + "\n", encoding="utf-8"
        )
        _write_manifest(
            args.output_directory,
            {**common, "mode": args.mode, "sample_rate": rate, "instruction": DEEP_MALE_DESIGN},
        )
        return 0

    if args.script is None:
        raise ValueError("custom and clone modes require --script")
    events = parse_script(args.script.read_text(encoding="utf-8"))
    texts = [event.text for event in events if event.kind == "speech"]
    if not texts or any(text is None for text in texts):
        raise ValueError("script contains no valid speech")

    clone_prompt = None
    if args.mode == "clone":
        if args.reference_audio is None or not args.reference_audio.is_file():
            raise ValueError("clone mode requires --reference-audio")
        clone_prompt = model.create_voice_clone_prompt(
            ref_audio=str(args.reference_audio.resolve()),
            ref_text=args.reference_text,
            x_vector_only_mode=False,
        )

    records: list[dict[str, object]] = []
    for index, maybe_text in enumerate(texts, start=1):
        assert maybe_text is not None
        seed = args.seed + index
        torch.manual_seed(seed)
        if args.mode == "custom":
            wavs, rate = model.generate_custom_voice(
                text=maybe_text,
                language="English",
                speaker=args.speaker,
                instruct=CUSTOM_INSTRUCTION,
                max_new_tokens=2048,
            )
        else:
            wavs, rate = model.generate_voice_clone(
                text=maybe_text,
                language="English",
                voice_clone_prompt=clone_prompt,
                max_new_tokens=2048,
            )
        output = args.output_directory / f"speech-{index:03d}.wav"
        sf.write(output, wavs[0], rate, subtype="PCM_16")
        duration = len(wavs[0]) / rate
        word_count = len(maybe_text.split())
        if duration > max(45.0, word_count * 1.5):
            raise RuntimeError(
                f"section {index} produced implausible {duration:.2f}s for {word_count} words"
            )
        records.append(
            {
                "section": index,
                "seed": seed,
                "words": word_count,
                "duration_seconds": duration,
                "sample_rate": rate,
                "path": str(output.resolve()),
            }
        )

    _write_manifest(
        args.output_directory,
        {
            **common,
            "mode": args.mode,
            "speaker": args.speaker if args.mode == "custom" else None,
            "instruction": CUSTOM_INSTRUCTION if args.mode == "custom" else None,
            "reference_audio": (
                str(args.reference_audio.resolve()) if args.reference_audio is not None else None
            ),
            "sections": records,
        },
    )
    del model
    gc.collect()
    if args.device == "mps":
        torch.mps.empty_cache()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
