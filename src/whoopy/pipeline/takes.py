"""Immutable alternative speech takes backed by production segment checkpoints."""

from __future__ import annotations

import hashlib
import json
import shutil
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import processed_speech_metadata
from whoopy.audio.synthesis import cache_key_for_identity
from whoopy.pipeline.checkpoints import SegmentCheckpoint, SegmentCheckpointStore
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.runs import RunStatus, RunStore, RunStoreError
from whoopy.timeline import SpeechSegment

TAKES_ROOT = "takes"
ACTIVE_TAKE_FILENAME = "active.json"


class SegmentTakeError(RunStoreError):
    """Raised when immutable take evidence cannot be captured or selected."""


class SegmentTakeManifest(BaseModel):
    """Immutable identity and production provenance for one synthesized take."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    take_id: str = Field(pattern=r"^t[0-9]{4}-[0-9a-f]{10}$")
    run_id: UUID
    segment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    created_at: AwareDatetime
    source: Literal["original", "production_regeneration"]
    regeneration_attempt_id: UUID | None = None
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    pcm_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_rate: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    byte_count: int = Field(gt=0)


class TakeSelection(BaseModel):
    """Immutable choose/undo audit record; active.json is only a pointer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    selection_id: str = Field(pattern=r"^s[0-9]{4}-[0-9a-f]{10}$")
    run_id: UUID
    segment_id: str
    selected_at: AwareDatetime
    previous_take_id: str | None
    selected_take_id: str
    action: Literal["choose", "undo", "restore"]


class SegmentTakeStore:
    """Capture, select, and restore verified checkpoint audio without synthesis."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.checkpoints = SegmentCheckpointStore(store)

    def list(self, run_id: UUID | str, segment_id: str) -> list[SegmentTakeManifest]:
        root = self._segment_root(run_id, segment_id)
        manifests: list[SegmentTakeManifest] = []
        for path in sorted(root.glob("t*/take.json")):
            try:
                manifests.append(SegmentTakeManifest.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        return manifests

    def selected(self, run_id: UUID | str, segment_id: str) -> str | None:
        try:
            payload = json.loads(
                (self._segment_root(run_id, segment_id) / ACTIVE_TAKE_FILENAME).read_text("utf-8")
            )
            take_id = payload["take_id"]
            if not isinstance(take_id, str):
                return None
            return take_id
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def capture_active(
        self,
        run_id: UUID | str,
        segment_id: str,
        *,
        source: Literal["original", "production_regeneration"],
        regeneration_attempt_id: UUID | None = None,
        select_if_unset: bool = True,
    ) -> SegmentTakeManifest:
        parsed = self.store.parse_run_id(run_id)
        self._validate_speech_segment(parsed, segment_id)
        try:
            with RunLock(self.store.run_directory(parsed)):
                checkpoint = self.checkpoints.load_optional(parsed, segment_id)
                if checkpoint is None:
                    raise SegmentTakeError("Speech segment has no production checkpoint.")
                completed = self.checkpoints.load_completed(
                    parsed,
                    segment_id,
                    cache_key=checkpoint.cache_key,
                )
                if completed is None:
                    raise SegmentTakeError(
                        "Speech segment checkpoint audio is incomplete or corrupt."
                    )
                root = self._segment_root(parsed, segment_id)
                existing = self.list(parsed, segment_id)
                take_id = f"t{len(existing) + 1:04d}-{uuid4().hex[:10]}"
                destination = root / take_id
                staging = root / f".{take_id}.tmp"
                manifest = self._write_take(
                    staging,
                    destination,
                    take_id=take_id,
                    run_id=parsed,
                    segment_id=segment_id,
                    source=source,
                    regeneration_attempt_id=regeneration_attempt_id,
                    checkpoint=completed.checkpoint,
                    audio=completed.audio,
                )
                if select_if_unset and self.selected(parsed, segment_id) is None:
                    self._write_pointer(root, take_id)
                return manifest
        except RunLockUnavailable as error:
            raise SegmentTakeError(f"Run {parsed} is owned by an active worker.") from error

    def choose(
        self,
        run_id: UUID | str,
        segment_id: str,
        take_id: str,
        *,
        action: Literal["choose", "undo", "restore"] = "choose",
    ) -> TakeSelection:
        parsed = self.store.parse_run_id(run_id)
        try:
            with RunLock(self.store.run_directory(parsed)):
                record = self.store.load(parsed)
                if record.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    raise SegmentTakeError("Wait for generation to stop before choosing a take.")
                manifest, checkpoint, audio = self._load_take(parsed, segment_id, take_id)
                segment = self._validate_speech_segment(parsed, segment_id)
                if record.source_kind == "fixture_prompt":
                    identity = FixtureSpeechSynthesizer.cache_identity
                else:
                    metadata = self.store.load_model_metadata(parsed)
                    config = self.store.load_resolved_config(parsed)
                    identity = processed_speech_metadata(
                        metadata.tts,
                        inner_cache_identity=metadata.tts.cache_identity,
                        settings=config.processing,
                    ).cache_identity
                expected_key = cache_key_for_identity(
                    segment,
                    synthesizer_identity=identity,
                    sample_rate=manifest.sample_rate,
                )
                if checkpoint.cache_key != expected_key or manifest.cache_key != expected_key:
                    raise SegmentTakeError("Take is incompatible with the current speech segment.")
                previous = self.selected(parsed, segment_id)
                self.checkpoints.save(checkpoint, audio=audio)
                root = self._segment_root(parsed, segment_id)
                selections = sorted((root / "selections").glob("s*.json"))
                selection = TakeSelection(
                    selection_id=f"s{len(selections) + 1:04d}-{uuid4().hex[:10]}",
                    run_id=parsed,
                    segment_id=segment_id,
                    selected_at=datetime.now(UTC),
                    previous_take_id=previous,
                    selected_take_id=manifest.take_id,
                    action=action,
                )
                self._write_new(
                    root / "selections" / f"{selection.selection_id}.json",
                    (selection.model_dump_json(indent=2) + "\n").encode(),
                )
                self._write_pointer(root, manifest.take_id)
                return selection
        except RunLockUnavailable as error:
            raise SegmentTakeError(f"Run {parsed} is owned by an active worker.") from error

    def undo(self, run_id: UUID | str, segment_id: str) -> TakeSelection:
        parsed = self.store.parse_run_id(run_id)
        root = self._segment_root(parsed, segment_id)
        paths = sorted((root / "selections").glob("s*.json"))
        if not paths:
            raise SegmentTakeError("There is no take selection to undo.")
        try:
            latest = TakeSelection.model_validate_json(paths[-1].read_text("utf-8"))
        except (OSError, ValueError) as error:
            raise SegmentTakeError(f"Could not read take selection history: {error}") from error
        if latest.previous_take_id is None:
            raise SegmentTakeError("The earliest selected take cannot be undone.")
        return self.choose(
            parsed,
            segment_id,
            latest.previous_take_id,
            action="undo",
        )

    def audio_path(self, run_id: UUID | str, segment_id: str, take_id: str) -> Path:
        manifest, _checkpoint, _audio = self._load_take(
            self.store.parse_run_id(run_id), segment_id, take_id
        )
        return self._segment_root(manifest.run_id, segment_id) / take_id / "audio.wav"

    def _load_take(
        self,
        run_id: UUID,
        segment_id: str,
        take_id: str,
    ) -> tuple[SegmentTakeManifest, SegmentCheckpoint, PcmAudio]:
        if not take_id.startswith("t") or "/" in take_id or ".." in take_id:
            raise SegmentTakeError("Invalid take ID.")
        directory = self._segment_root(run_id, segment_id) / take_id
        try:
            manifest = SegmentTakeManifest.model_validate_json(
                (directory / "take.json").read_text("utf-8")
            )
            checkpoint_payload = (directory / "checkpoint.json").read_bytes()
            checkpoint = SegmentCheckpoint.model_validate_json(checkpoint_payload)
            pcm = (directory / "audio.pcm").read_bytes()
        except (OSError, ValueError) as error:
            raise SegmentTakeError(f"Could not load take {take_id}: {error}") from error
        if manifest.run_id != run_id or manifest.segment_id != segment_id:
            raise SegmentTakeError("Take does not belong to this run segment.")
        if hashlib.sha256(checkpoint_payload).hexdigest() != manifest.checkpoint_sha256:
            raise SegmentTakeError("Take checkpoint digest does not match its manifest.")
        if hashlib.sha256(pcm).hexdigest() != manifest.pcm_sha256:
            raise SegmentTakeError("Take audio digest does not match its manifest.")
        audio = PcmAudio(pcm_s16le=pcm, sample_rate=manifest.sample_rate)
        if audio.frame_count != manifest.frame_count or len(pcm) != manifest.byte_count:
            raise SegmentTakeError("Take audio dimensions do not match its manifest.")
        return manifest, checkpoint, audio

    def _write_take(
        self,
        staging: Path,
        destination: Path,
        *,
        take_id: str,
        run_id: UUID,
        segment_id: str,
        source: Literal["original", "production_regeneration"],
        regeneration_attempt_id: UUID | None,
        checkpoint: SegmentCheckpoint,
        audio: PcmAudio,
    ) -> SegmentTakeManifest:
        checkpoint_payload = (checkpoint.model_dump_json(indent=2) + "\n").encode()
        manifest = SegmentTakeManifest(
            take_id=take_id,
            run_id=run_id,
            segment_id=segment_id,
            created_at=datetime.now(UTC),
            source=source,
            regeneration_attempt_id=regeneration_attempt_id,
            cache_key=checkpoint.cache_key,
            pcm_sha256=hashlib.sha256(audio.pcm_s16le).hexdigest(),
            checkpoint_sha256=hashlib.sha256(checkpoint_payload).hexdigest(),
            sample_rate=audio.sample_rate,
            frame_count=audio.frame_count,
            byte_count=len(audio.pcm_s16le),
        )
        try:
            staging.mkdir(parents=True)
            (staging / "audio.pcm").write_bytes(audio.pcm_s16le)
            (staging / "checkpoint.json").write_bytes(checkpoint_payload)
            with wave.open(str(staging / "audio.wav"), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(audio.sample_rate)
                output.writeframes(audio.pcm_s16le)
            (staging / "take.json").write_text(
                manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            staging.replace(destination)
            return manifest
        except OSError as error:
            raise SegmentTakeError(f"Could not save immutable take: {error}") from error
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _validate_speech_segment(self, run_id: UUID, segment_id: str) -> SpeechSegment:
        matches = [
            segment
            for segment in self.store.load_timeline(run_id).segments
            if segment.id == segment_id
        ]
        if not matches:
            raise SegmentTakeError(f"Segment {segment_id} is not present in run {run_id}.")
        if not isinstance(matches[0], SpeechSegment):
            raise SegmentTakeError(f"Segment {segment_id} is not a speech segment.")
        return matches[0]

    def _segment_root(self, run_id: UUID | str, segment_id: str) -> Path:
        # Reuse the checkpoint store's segment-ID validation before constructing
        # the parallel immutable-takes path.
        self.checkpoints.segment_directory(run_id, segment_id)
        return self.store.run_directory(run_id) / TAKES_ROOT / segment_id

    def _write_pointer(self, root: Path, take_id: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps({"take_id": take_id}, indent=2) + "\n").encode()
        temporary = root / f".{ACTIVE_TAKE_FILENAME}.{uuid4().hex}.tmp"
        temporary.write_bytes(payload)
        temporary.replace(root / ACTIVE_TAKE_FILENAME)

    @staticmethod
    def _write_new(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise SegmentTakeError(f"Refusing to replace immutable take evidence: {path}")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
