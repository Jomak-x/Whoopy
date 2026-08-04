"""Optional Apache-2.0 MOSS-TTS v1.5 adapters for capable local Macs."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from whoopy.audio.models import SAMPLE_RATE, PcmAudio
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    InvalidSynthesisOutput,
    TransientSynthesisError,
)
from whoopy.ports import AdapterMetadata
from whoopy.timeline import SpeechSegment

MossVariant = Literal["moss-local-v1.5", "moss-v1.5"]
MOSS_LANGUAGES = (
    "Arabic",
    "Cantonese",
    "Chinese",
    "Czech",
    "Danish",
    "Dutch",
    "English",
    "Finnish",
    "French",
    "German",
    "Greek",
    "Hebrew",
    "Hindi",
    "Hungarian",
    "Italian",
    "Japanese",
    "Korean",
    "Macedonian",
    "Malay",
    "Persian (Farsi)",
    "Polish",
    "Portuguese",
    "Romanian",
    "Russian",
    "Spanish",
    "Swahili",
    "Swedish",
    "Tagalog",
    "Thai",
    "Turkish",
    "Vietnamese",
)


def _checkpoint_is_complete(directory: Path) -> bool:
    """Accept a complete single-file or sharded safetensors checkpoint only."""
    single_file = directory / "model.safetensors"
    if single_file.is_file() and single_file.stat().st_size > 0:
        return True

    index_file = directory / "model.safetensors.index.json"
    if not index_file.is_file():
        return False
    try:
        index = json.loads(index_file.read_text(encoding="utf-8"))
        shards = set(index["weight_map"].values())
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(shards) and all(
        (directory / shard).is_file() and (directory / shard).stat().st_size > 0 for shard in shards
    )


@dataclass(frozen=True)
class MossTTSSettings:
    runtime_directory: Path
    worker_script: Path
    model_directory: Path
    codec_directory: Path
    variant: MossVariant
    reference_audio: Path
    language: str = "English"
    instruction: str = "Speak slowly, softly, and warmly, with a meditative delivery."
    use_reference: bool = True
    seed: int = 42


class MossTTSAdapter:
    """Map either MOSS v1.5 architecture to Whoopy's PCM contract."""

    sample_rate: int = SAMPLE_RATE

    def __init__(self, settings: MossTTSSettings) -> None:
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        reference_hash = (
            hashlib.sha256(settings.reference_audio.read_bytes()).hexdigest()
            if settings.reference_audio.is_file()
            else "missing"
        )
        model_id = (
            "MOSS-TTS-Local-Transformer@1.5"
            if settings.variant == "moss-local-v1.5"
            else "MOSS-TTS@1.5"
        )
        self.metadata = AdapterMetadata(
            adapter_id=f"whoopy.{settings.variant}",
            versioned_model_id=model_id,
            runtime_id="transformers-isolated-python",
            runtime_version="5.0.0",
            license_id="Apache-2.0",
            device="mps-or-cpu",
            settings=(
                f"language={settings.language}",
                f"instruction={settings.instruction}",
                f"use_reference={settings.use_reference}",
                f"reference_sha256={reference_hash}",
                f"seed={settings.seed}",
                f"sample_rate={self.sample_rate}",
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    @staticmethod
    def availability_error(
        runtime_directory: Path,
        model_directory: Path,
        codec_directory: Path,
        reference_audio: Path,
    ) -> str | None:
        python = runtime_directory / ".venv" / "bin" / "python"
        missing: list[str] = []
        if not python.is_file():
            missing.append("isolated Python runtime")
        if not (model_directory / "config.json").is_file() or not _checkpoint_is_complete(
            model_directory
        ):
            missing.append("model checkpoint")
        if not (codec_directory / "config.json").is_file() or not _checkpoint_is_complete(
            codec_directory
        ):
            missing.append("audio tokenizer")
        if not reference_audio.is_file():
            missing.append("reference voice")
        return None if not missing else f"missing {', '.join(missing)}"

    def _start(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        error = self.availability_error(
            self.settings.runtime_directory,
            self.settings.model_directory,
            self.settings.codec_directory,
            self.settings.reference_audio,
        )
        if error is not None:
            raise FatalSynthesisError(f"{self.settings.variant} is not ready: {error}.")
        command = [
            str(self.settings.runtime_directory / ".venv" / "bin" / "python"),
            str(self.settings.worker_script),
            "--model",
            str(self.settings.model_directory),
            "--codec",
            str(self.settings.codec_directory),
            "--reference-audio",
            str(self.settings.reference_audio),
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                bufsize=1,
            )
            assert self._process.stdout is not None
            ready = json.loads(self._process.stdout.readline())
        except (OSError, ValueError, json.JSONDecodeError) as error:
            self.close()
            raise FatalSynthesisError(f"Could not start MOSS-TTS: {error}") from error
        if ready.get("status") != "ready":
            self.close()
            raise FatalSynthesisError(f"MOSS-TTS did not become ready: {ready}")
        return self._process

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        process = self._start()
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                json.dumps(
                    {
                        "text": segment.text,
                        "seed": self.settings.seed,
                        "language": self.settings.language,
                        "instruction": self.settings.instruction,
                        "use_reference": self.settings.use_reference,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
        except (BrokenPipeError, OSError, json.JSONDecodeError) as error:
            self.close()
            raise TransientSynthesisError(f"MOSS-TTS process failed: {error}") from error
        if response.get("status") != "ok":
            raise TransientSynthesisError(
                f"MOSS-TTS generation failed: {response.get('error', 'unknown error')}"
            )
        try:
            pcm = base64.b64decode(response["pcm_s16le"], validate=True)
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidSynthesisOutput("MOSS-TTS returned invalid PCM data") from error
        if not pcm:
            raise InvalidSynthesisOutput("MOSS-TTS returned empty audio")
        return PcmAudio(pcm_s16le=pcm, sample_rate=self.sample_rate)

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def __del__(self) -> None:
        self.close()
