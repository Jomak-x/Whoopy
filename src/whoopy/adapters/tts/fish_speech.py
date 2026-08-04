"""Optional non-commercial Fish Speech 1.4 adapter for local Mac experiments."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from whoopy.audio.models import SAMPLE_RATE, PcmAudio
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    InvalidSynthesisOutput,
    TransientSynthesisError,
)
from whoopy.ports import AdapterMetadata
from whoopy.timeline import SpeechSegment


@dataclass(frozen=True)
class FishSpeechSettings:
    """Paths and reproducibility controls for the optional Fish installation."""

    runtime_directory: Path
    worker_script: Path
    reference_audio: Path
    reference_text: Path
    seed: int = 42


class FishSpeech14Adapter:
    """Use Fish 1.4 behind Whoopy's ordinary speech-synthesizer contract."""

    sample_rate: int = SAMPLE_RATE

    def __init__(self, settings: FishSpeechSettings) -> None:
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        reference_hash = hashlib.sha256()
        for path in (settings.reference_audio, settings.reference_text):
            if path.is_file():
                reference_hash.update(path.read_bytes())
            else:
                reference_hash.update(str(path).encode())
        self.metadata = AdapterMetadata(
            adapter_id="whoopy.fish_speech_1_4",
            versioned_model_id="Fish-Speech@1.4",
            runtime_id="fish-speech-isolated-python",
            runtime_version="1.4",
            license_id="CC-BY-NC-SA-4.0",
            device="mps",
            settings=(
                f"reference_audio={settings.reference_audio.name}",
                f"reference_text={settings.reference_text.name}",
                f"reference_sha256={reference_hash.hexdigest()}",
                f"seed={settings.seed}",
                f"sample_rate={self.sample_rate}",
                "expression_control=reference_audio",
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    @staticmethod
    def availability_error(runtime_directory: Path) -> str | None:
        """Return a human-readable missing component instead of loading a model."""

        required = (
            runtime_directory / ".venv" / "bin" / "python",
            runtime_directory / "checkpoints" / "fish-speech-1.4" / "model.pth",
            runtime_directory
            / "checkpoints"
            / "fish-speech-1.4"
            / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth",
            runtime_directory / "whoopy-reference.wav",
            runtime_directory / "whoopy-reference.txt",
        )
        missing = [path.name for path in required if not path.is_file()]
        return None if not missing else f"missing {', '.join(missing)}"

    def _start(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        runtime = self.settings.runtime_directory
        error = self.availability_error(runtime)
        if error is not None:
            raise FatalSynthesisError(f"Fish Speech 1.4 is not ready: {error}.")
        command = [
            str(runtime / ".venv" / "bin" / "python"),
            str(self.settings.worker_script),
            "--runtime",
            str(runtime),
            "--reference-audio",
            str(self.settings.reference_audio),
            "--reference-text",
            str(self.settings.reference_text),
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
            raise FatalSynthesisError(f"Could not start Fish Speech 1.4: {error}") from error
        if ready.get("status") != "ready":
            self.close()
            raise FatalSynthesisError(f"Fish Speech 1.4 did not become ready: {ready}")
        return self._process

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        process = self._start()
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                json.dumps(
                    {"text": segment.text, "seed": self.settings.seed},
                    separators=(",", ":"),
                )
                + "\n"
            )
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
        except (BrokenPipeError, OSError, json.JSONDecodeError) as error:
            self.close()
            raise TransientSynthesisError(f"Fish Speech process failed: {error}") from error
        if response.get("status") != "ok":
            raise TransientSynthesisError(
                f"Fish Speech generation failed: {response.get('error', 'unknown error')}"
            )
        try:
            pcm = base64.b64decode(response["pcm_s16le"], validate=True)
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidSynthesisOutput("Fish Speech returned invalid PCM data") from error
        if not pcm:
            raise InvalidSynthesisOutput("Fish Speech returned empty audio")
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
