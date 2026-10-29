"""Bridge Evaluation Lab trials to Whoopy's ordinary durable generation path."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, cast
from uuid import UUID

from whoopy.evaluation import (
    ArtifactReference,
    CanonicalSourceProvenance,
    ControlledInputFingerprints,
    EvaluationExecutionError,
    ExperimentDefinition,
    MetricValue,
    ProductionTrialRequest,
    ProductionTrialResult,
)
from whoopy.evaluation.pacing_profiles import (
    PacingProfile,
    PacingProfileError,
    PacingProfileRegistry,
)
from whoopy.meditation.models import DraftedSection, MeditationKind, MeditationPlan
from whoopy.meditation.pacing import allocate_bounded_quiet
from whoopy.meditation.recipes import load_recipe_library, route_session
from whoopy.meditation.whole_session import recipe_quiet_limits
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.ports import AdapterMetadata
from whoopy.timeline import Timeline
from whoopy.webui.server import LocalWebApplication


class LocalProductionExperimentExecutor:
    """Execute trials via the same durable child-process workflow as Studio."""

    PRODUCTION_WRITING_METHODS = (
        "plan_first",
        "blueprint_first_sparse",
        "three_candidate_structural_selection",
        "recipe_whole_session",
    )
    LLM_PROFILES: ClassVar[dict[str, str]] = {
        "qwen3-4b": "standard",
        "qwen3-4b-standard": "standard",
        "qwen3-1.7b": "lite",
        "qwen3-1.7b-lite": "lite",
        "ministral-3-14b": "high",
        "ministral-3-14b-high": "high",
    }
    LLM_METADATA_MARKERS: ClassVar[dict[str, tuple[str, ...]]] = {
        "standard": ("qwen3 4b", "qwen3-4b"),
        "lite": ("qwen3 1.7b", "qwen3-1.7b"),
        "high": ("ministral 3 14b", "ministral-3-14b"),
    }
    VOICE_BACKENDS: ClassVar[dict[str, str]] = {
        "kokoro": "kokoro",
        "kokoro@1": "kokoro",
        "fish": "fish-1.4",
        "fish-1.4": "fish-1.4",
        "moss-5b": "moss-local-v1.5",
        "moss-local-v1.5": "moss-local-v1.5",
        "moss-8b": "moss-v1.5",
        "moss-v1.5": "moss-v1.5",
        "qwen3-custom-voice-0.6b": "qwen3-custom-voice-0.6b",
        "qwen3-custom-voice-1.7b": "qwen3-custom-voice-1.7b",
        "qwen3-voice-design-1.7b": "qwen3-voice-design-1.7b",
        "qwen3-base-clone-0.6b": "qwen3-base-clone-0.6b",
        "qwen3-base-clone-1.7b": "qwen3-base-clone-1.7b",
    }

    @classmethod
    def capabilities(cls, *, execution_available: bool = True) -> dict[str, Any]:
        """Describe what the ordinary generation pipeline can truthfully compare."""

        return {
            "schema_version": 1,
            "execution_available": execution_available,
            "writing_methods": [
                {
                    "id": "recipe_whole_session",
                    "label": "Recipe-guided whole-session drafting",
                    "available": execution_available,
                    "production_backed": True,
                    "reason": None
                    if execution_available
                    else "No production executor is configured.",
                },
                {
                    "id": "blueprint_first_sparse",
                    "label": "Blueprint-first sparse narration",
                    "available": execution_available,
                    "production_backed": True,
                    "reason": (
                        None
                        if execution_available
                        else "No production evaluation executor is configured."
                    ),
                },
                {
                    "id": "plan_first",
                    "label": "Legacy plan-first drafting",
                    "available": execution_available,
                    "production_backed": True,
                    "reason": None
                    if execution_available
                    else "No production executor is configured.",
                },
                {
                    "id": "three_candidate_structural_selection",
                    "label": "Three-candidate structural selection",
                    "available": execution_available,
                    "production_backed": True,
                    "reason": None
                    if execution_available
                    else "No production executor is configured.",
                },
            ],
            "experiment_types": {
                "llm": {"available": execution_available, "reason": None},
                "writing_method": {
                    "available": execution_available,
                    "reason": None,
                },
                "voice": {"available": execution_available, "reason": None},
                "delivery": {"available": execution_available, "reason": None},
                "pacing": {"available": execution_available, "reason": None},
            },
        }

    @classmethod
    def validate_definition(cls, definition: ExperimentDefinition) -> None:
        """Reject definitions that would inevitably become failed production trials."""

        requested = {candidate.condition.writing_method for candidate in definition.candidates}
        unsupported = sorted(requested - set(cls.PRODUCTION_WRITING_METHODS))
        if unsupported:
            raise EvaluationExecutionError(
                "Experiment uses writing methods without a durable production route: "
                f"{unsupported}. Production-backed methods are "
                f"{list(cls.PRODUCTION_WRITING_METHODS)!r}."
            )
        if definition.experiment_type == "pacing":
            backend = cls.VOICE_BACKENDS.get(
                definition.candidates[0].condition.voice.model_id.casefold()
            )
            if backend != "kokoro":
                raise EvaluationExecutionError(
                    "Pacing experiments currently require Kokoro because it is the only "
                    "production adapter that exposes Whoopy's speech-speed control."
                )
        if definition.experiment_type == "voice":
            voices = [candidate.condition.voice for candidate in definition.candidates]
            baseline = voices[0]
            unsupported_changes = sorted(
                field
                for field in ("delivery", "intensity", "language", "voice_mode")
                if any(getattr(voice, field) != getattr(baseline, field) for voice in voices[1:])
            )
            if unsupported_changes:
                raise EvaluationExecutionError(
                    "Voice experiments currently compare models, presets, or references while "
                    "typed delivery, intensity, language, and voice mode stay fixed. Separate "
                    "delivery-preset comparisons belong in a Delivery experiment; changed fields: "
                    f"{unsupported_changes}."
                )
        if definition.experiment_type == "delivery":
            voices = [candidate.condition.voice for candidate in definition.candidates]
            backend = cls.VOICE_BACKENDS.get(voices[0].model_id.casefold())
            expressive = backend is not None and (
                backend.startswith("moss-")
                or backend == "qwen3-custom-voice-1.7b"
                or backend == "qwen3-voice-design-1.7b"
            )
            if not expressive:
                raise EvaluationExecutionError(
                    "Delivery experiments require an expressive MOSS, Qwen CustomVoice, "
                    "or Qwen VoiceDesign production adapter; the selected adapter exposes "
                    "no audible delivery-preset control."
                )

    def __init__(
        self,
        application: LocalWebApplication,
        *,
        poll_seconds: float = 0.2,
        timeout_seconds: float = 14_400,
        pacing_profiles: PacingProfileRegistry | None = None,
        active_run_changed: Callable[[UUID | None], None] | None = None,
    ) -> None:
        self.application = application
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self.active_run_changed = active_run_changed
        config_directory = Path(getattr(application, "config_directory", Path("config")))
        profile_path = config_directory / "pacing_profiles.yaml"
        self.pacing_profiles = pacing_profiles or (
            PacingProfileRegistry.load(profile_path)
            if profile_path.is_file()
            else PacingProfileRegistry.load_bundled()
        )

    def execute(self, request: ProductionTrialRequest) -> ProductionTrialResult:
        started_at = datetime.now(UTC)
        started_clock = time.monotonic()
        pinned_source: CanonicalSourceProvenance | None = None
        try:
            if request.controlled_source_run_id is not None:
                pinned_source, _timeline = self._capture_canonical_source(
                    request.controlled_source_run_id
                )
            payload = self._generation_payload(request)
            if pinned_source is not None:
                self._verify_canonical_source(pinned_source)
        except (OSError, ValueError) as error:
            return self._durable_failure(request, started_at, str(error))
        try:
            task = self.application.start_generation(payload, _run_created=self.active_run_changed)
        except (OSError, ValueError) as error:
            return self._durable_failure(request, started_at, str(error))

        return self._wait_for_task(
            request,
            task,
            started_at=started_at,
            started_clock=started_clock,
            pinned_source=pinned_source,
        )

    def resume(self, request: ProductionTrialRequest, run_id: UUID) -> ProductionTrialResult:
        """Recover an existing run and preserve its trial identity."""

        started_at = datetime.now(UTC)
        pinned_source: CanonicalSourceProvenance | None = None
        try:
            if request.controlled_source_run_id is not None:
                pinned_source, _timeline = self._capture_canonical_source(
                    request.controlled_source_run_id
                )
            task = self.application.task(str(run_id))
            if task is not None and task["status"] == "interrupted":
                task = self.application.resume_run(str(run_id))
        except (OSError, ValueError) as error:
            return self._failed_result(
                request,
                source_run_id=run_id,
                started_at=started_at,
                failure=f"Could not resume production trial: {error}",
            )
        if task is None:
            return self._failed_result(
                request,
                source_run_id=run_id,
                started_at=started_at,
                failure="Interrupted production run no longer exists.",
            )
        return self._wait_for_task(
            request,
            task,
            started_at=started_at,
            started_clock=None,
            pinned_source=pinned_source,
        )

    def _wait_for_task(
        self,
        request: ProductionTrialRequest,
        task: dict[str, Any],
        *,
        started_at: datetime,
        started_clock: float | None,
        pinned_source: CanonicalSourceProvenance | None,
    ) -> ProductionTrialResult:
        wait_started_clock = time.monotonic() if started_clock is None else started_clock
        task_id = str(task["task_id"])
        initial_run_id = UUID(str(task["run_id"]))
        if self.active_run_changed is not None:
            self.active_run_changed(initial_run_id)
        while True:
            state = self.application.task(task_id)
            if state is None:
                result = self._failed_result(
                    request,
                    source_run_id=initial_run_id,
                    started_at=started_at,
                    failure="The durable production task disappeared before completion.",
                )
                if self.active_run_changed is not None:
                    self.active_run_changed(None)
                return result
            if state["status"] not in {"queued", "running"}:
                break
            if time.monotonic() - wait_started_clock > self.timeout_seconds:
                self.application.cancel_task(task_id)
                result = self._failed_result(
                    request,
                    source_run_id=UUID(str(state["run_id"])),
                    started_at=started_at,
                    failure=f"Production trial exceeded {self.timeout_seconds:g} seconds.",
                )
                if self.active_run_changed is not None:
                    self.active_run_changed(None)
                return result
            time.sleep(self.poll_seconds)

        run_id = UUID(str(state["run_id"]))
        if self.active_run_changed is not None:
            self.active_run_changed(None)
        if state["status"] != "completed":
            return self._failed_result(
                request,
                source_run_id=run_id,
                started_at=started_at,
                failure=str(state.get("error") or f"Production run ended as {state['status']}"),
            )
        try:
            evidence_lock = self._acquire_finalized_run_lock(run_id, wait_started_clock)
        except TimeoutError as error:
            return self._failed_result(
                request,
                source_run_id=run_id,
                started_at=started_at,
                failure=str(error),
            )
        try:
            try:
                return self._completed_result(
                    request,
                    run_id,
                    started_at,
                    started_clock,
                    pinned_source=pinned_source,
                )
            finally:
                evidence_lock.release()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return self._failed_result(
                request,
                source_run_id=run_id,
                started_at=started_at,
                failure=f"Completed run evidence is invalid: {error}",
            )

    def _acquire_finalized_run_lock(self, run_id: UUID, started_clock: float) -> RunLock:
        """Hold the run lock while hashing evidence after worker finalization.

        A durable run becomes ``completed`` before the worker's ``finally`` block
        persists final telemetry. The run lock spans that block, so acquiring it
        is the authoritative boundary; a timing sleep or repeated stat cannot
        provide the same guarantee on slow filesystems.
        """

        lock = RunLock(self.application.runs_directory / str(run_id))
        while True:
            try:
                lock.acquire()
                return lock
            except RunLockUnavailable as error:
                if time.monotonic() - started_clock > self.timeout_seconds:
                    raise TimeoutError(
                        "Production run completed but its worker did not release the run lock "
                        f"within {self.timeout_seconds:g} seconds."
                    ) from error
                time.sleep(self.poll_seconds)

    FALLBACK_DELIVERY_BACKENDS = frozenset(
        {"qwen3-custom-voice-0.6b", "qwen3-base-clone-0.6b", "qwen3-base-clone-1.7b"}
    )

    @classmethod
    def _effective_delivery(
        cls, backend: str | None, delivery: str, intensity: str
    ) -> tuple[str, str]:
        if backend in cls.FALLBACK_DELIVERY_BACKENDS:
            return "neutral", "subtle"
        return delivery, intensity

    def _generation_payload(self, request: ProductionTrialRequest) -> dict[str, Any]:
        condition = request.condition
        llm = condition.llm_model_id.casefold()
        profile = self.LLM_PROFILES.get(llm)
        if profile is None:
            raise ValueError(
                f"LLM {condition.llm_model_id!r} is not exposed by the production generator."
            )
        if condition.writing_method not in self.PRODUCTION_WRITING_METHODS:
            raise ValueError(
                f"Writing method {condition.writing_method!r} is not implemented in production."
            )
        voice = condition.voice
        model = voice.model_id.casefold()
        backend = self.VOICE_BACKENDS.get(model)
        if backend is None:
            raise ValueError(f"Voice model {voice.model_id!r} is not exposed by production.")
        if backend == "kokoro":
            if voice.voice_mode != "preset":
                raise ValueError("Kokoro production trials require preset voice mode.")
            if voice.language.casefold() != "english":
                raise ValueError("Kokoro production trials currently require English.")
        elif backend == "fish-1.4":
            if voice.voice_mode != "reference":
                raise ValueError("Fish production trials require consented reference voice mode.")
            if voice.language.casefold() != "english":
                raise ValueError("Fish production trials currently require English.")
            self._validate_managed_reference(voice.voice_id)
        elif backend.startswith("moss-"):
            if voice.voice_mode not in {"reference", "designed"}:
                raise ValueError("MOSS production trials require reference or direct voice mode.")
            if voice.voice_mode == "reference":
                self._validate_managed_reference(voice.voice_id)
        elif backend == "qwen3-voice-design-1.7b":
            if voice.voice_mode != "designed":
                raise ValueError("Qwen VoiceDesign trials require designed voice mode.")
        elif backend.startswith("qwen3-base-clone-"):
            if voice.voice_mode != "reference":
                raise ValueError("Qwen Base trials require consented reference voice mode.")
            self._validate_managed_reference(voice.voice_id)
        elif voice.voice_mode != "preset":
            raise ValueError("Qwen CustomVoice trials require preset voice mode.")
        if voice.intensity != "subtle" and not (
            backend.startswith("moss-")
            or backend.startswith("qwen3-custom-voice-")
            or backend == "qwen3-voice-design-1.7b"
        ):
            raise ValueError(
                f"{backend} does not expose intensity as a production control; use subtle."
            )
        try:
            pacing_profile = self.pacing_profiles.get(condition.pacing_profile_id)
        except PacingProfileError as error:
            raise ValueError(
                f"Pacing profile {condition.pacing_profile_id!r} is not exposed by production."
            ) from error
        speed = pacing_profile.speech_speed
        kind = request.case.category
        delivery, intensity = self._effective_delivery(backend, voice.delivery, voice.intensity)
        payload: dict[str, Any] = {
            "surface": "lab",
            "writing_method": condition.writing_method,
            "mode": "prompt",
            "text": request.case.prompt,
            "minutes": request.case.duration_seconds / 60,
            "tts_model": backend,
            "voice": voice.voice_id,
            "speed": speed,
            "pacing_profile_id": pacing_profile.profile_id,
            "meditation_kind": kind,
            "delivery": delivery,
            "intensity": intensity,
            "seed": request.seed,
            "profile": profile,
        }
        if backend in self.FALLBACK_DELIVERY_BACKENDS:
            payload["pace"] = "natural"
        if (
            request.experiment_type in {"voice", "delivery", "pacing"}
            and request.controlled_source_run_id is not None
        ):
            payload.update(
                {
                    "mode": "script",
                    "text": self._frozen_script(
                        request.controlled_source_run_id,
                        pacing_profile=(
                            pacing_profile if request.experiment_type == "pacing" else None
                        ),
                        delivery_controls=(
                            (voice.delivery, voice.intensity)
                            if request.experiment_type == "delivery"
                            else None
                        ),
                        target_seconds=request.case.duration_seconds,
                        candidate_speed=speed,
                    ),
                    "profile": "basic",
                    "writing_method": "plan_first",
                    "requested_duration_seconds": request.case.duration_seconds,
                }
            )
        if backend == "fish-1.4":
            payload["voice_reference"] = voice.voice_id
        elif backend.startswith("moss-"):
            payload.update(
                {
                    "moss_language": voice.language,
                    "moss_instruction": voice.instruction
                    or self._moss_instruction(voice.delivery, voice.intensity),
                    "moss_use_reference": voice.voice_mode == "reference",
                    "voice_reference": (
                        voice.voice_id if voice.voice_mode == "reference" else None
                    ),
                }
            )
        elif backend.startswith("qwen3-custom-voice-"):
            payload.update({"qwen_language": voice.language, "qwen_speaker": voice.voice_id})
        elif backend == "qwen3-voice-design-1.7b":
            payload.update(
                {
                    "qwen_language": voice.language,
                    "qwen_voice_design_preset": voice.voice_id,
                }
            )
        elif backend.startswith("qwen3-base-clone-"):
            payload.update({"qwen_language": voice.language, "qwen_reference": voice.voice_id})
        return payload

    def _frozen_script(
        self,
        source_run_id: UUID,
        *,
        pacing_profile: PacingProfile | None = None,
        delivery_controls: tuple[str, str] | None = None,
        target_seconds: int,
        candidate_speed: float,
    ) -> str:
        """Serialize one accepted canonical timeline without redrafting its speech."""

        root = self.application.runs_directory / str(source_run_id)
        timeline = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
        segments = timeline["segments"]
        silence_durations = [
            int(segment["duration_ms"]) for segment in segments if segment["type"] == "SILENCE"
        ]
        if pacing_profile is not None and pacing_profile.silence_strategy == "progressive_quiet":
            silence_durations = self._progressive_quiet_durations(
                root,
                source_durations=silence_durations,
                target_seconds=target_seconds,
                candidate_speed=candidate_speed,
            )
        silence_index = 0
        lines: list[str] = ["# Controlled Lab source", ""]
        for segment in segments:
            segment_type = segment["type"]
            if segment_type == "SPEECH":
                if segment.get("pronunciation_overrides"):
                    raise ValueError(
                        "Controlled Lab cloning does not yet serialize pronunciation overrides."
                    )
                segment_delivery = (
                    delivery_controls[0] if delivery_controls is not None else segment["delivery"]
                )
                segment_intensity = (
                    delivery_controls[1] if delivery_controls is not None else segment["intensity"]
                )
                lines.extend(
                    [
                        f"[delivery: {segment_delivery}]",
                        f"[intensity: {segment_intensity}]",
                        f"[pace: {segment['pace']}]",
                        f"[voice-mode: {segment['voice_mode']}]",
                        "",
                        str(segment["text"]),
                        "",
                    ]
                )
            elif segment_type == "SILENCE":
                duration_ms = silence_durations[silence_index]
                silence_index += 1
                lines.extend([f"[pause: {duration_ms}ms]", ""])
            elif segment_type == "BREATH":
                lines.extend(
                    [
                        f"[breath: {segment['phase']} {segment['duration_ms'] / 1000:g}s "
                        f"cycle={segment['cycle']} protocol={segment['protocol_id']}]",
                        "",
                    ]
                )
            else:
                raise ValueError(f"Unsupported controlled timeline segment {segment_type!r}.")
        return "\n".join(lines).strip()

    @staticmethod
    def _progressive_quiet_durations(
        source_root: Path,
        *,
        source_durations: list[int],
        target_seconds: int,
        candidate_speed: float,
    ) -> list[int]:
        """Redistribute a fixed total budget toward later quiet periods."""

        if not source_durations:
            return []
        manifest = json.loads((source_root / "audio-manifest.json").read_text(encoding="utf-8"))
        resolved = json.loads((source_root / "resolved-config.json").read_text(encoding="utf-8"))
        source_speed = float(resolved["tts"]["speed"])
        speech_ms = sum(
            float(segment["actual_duration_ms"])
            for segment in manifest["segments"]
            if segment["segment_type"] == "SPEECH"
        )
        estimated_speech_ms = round(speech_ms * source_speed / candidate_speed)
        breath_ms = sum(
            round(float(segment["actual_duration_ms"]))
            for segment in manifest["segments"]
            if segment["segment_type"] == "BREATH"
        )
        minimum_ms = 1_000
        budget = target_seconds * 1_000 - estimated_speech_ms - breath_ms
        if budget < minimum_ms * len(source_durations):
            raise ValueError(
                "Progressive Quiet cannot preserve the requested duration and minimum rests."
            )
        count = len(source_durations)
        weights = [
            max(1, duration - minimum_ms)
            * (1.0 + (0.35 * index / (count - 1) if count > 1 else 0.0))
            for index, duration in enumerate(source_durations)
        ]
        caps = [600_000] * count
        if (resolved.get("generation") or {}).get("writing_method") == "recipe_whole_session":
            source_timeline = Timeline.model_validate_json(
                (source_root / "timeline.json").read_text(encoding="utf-8")
            )
            limits = recipe_quiet_limits(
                source_timeline,
                MeditationPlan.model_validate_json(
                    (source_root / "plan.json").read_text(encoding="utf-8")
                ),
                [
                    DraftedSection.model_validate_json(path.read_text(encoding="utf-8"))
                    for path in (source_root / "draft-sections").glob("*.json")
                ],
            )
            caps = [
                limits[segment.id]
                for segment in source_timeline.segments
                if segment.type == "SILENCE"
            ]
        return allocate_bounded_quiet(budget, weights, caps)

    @staticmethod
    def _moss_instruction(delivery: str, intensity: str) -> str:
        strength = "gently" if intensity == "subtle" else "clearly"
        return f"Speak {strength}, with a {delivery} meditative delivery."

    def _validate_managed_reference(self, requested_reference_id: str) -> None:
        # The versioned reference manager is the production boundary used by
        # Studio and the CLI.  Loading through it verifies the active immutable
        # revision, audio/transcript hashes, and explicit cloning consent.  Lab
        # must not silently fall back to the historical single default in
        # ``voice_references.yaml`` because users can manage multiple consented
        # references in the permanent product.
        self.application.voice_reference_manager.load(requested_reference_id)

    def _durable_failure(
        self,
        request: ProductionTrialRequest,
        started_at: datetime,
        failure: str,
    ) -> ProductionTrialResult:
        record = self.application.record_failed_generation(
            f"Lab {request.experiment_id} / {request.case.id}: {request.case.prompt}",
            failure,
        )
        return self._failed_result(
            request,
            source_run_id=record.run_id,
            started_at=started_at,
            failure=failure,
        )

    @staticmethod
    def _failed_result(
        request: ProductionTrialRequest,
        *,
        source_run_id: UUID,
        started_at: datetime,
        failure: str,
    ) -> ProductionTrialResult:
        return ProductionTrialResult(
            trial_id=request.trial_id,
            source_run_id=source_run_id,
            controlled_source_run_id=request.controlled_source_run_id,
            candidate_id=request.candidate_id,
            case_id=request.case.id,
            success=False,
            actual_condition=request.condition,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            failure=failure[:2_000],
        )

    def _completed_result(
        self,
        request: ProductionTrialRequest,
        run_id: UUID,
        started_at: datetime,
        started_clock: float | None,
        *,
        pinned_source: CanonicalSourceProvenance | None = None,
    ) -> ProductionTrialResult:
        root = self.application.runs_directory / str(run_id)
        evidence_root = (
            self.application.runs_directory / str(request.controlled_source_run_id)
            if request.controlled_source_run_id is not None
            else root
        )
        artifacts = self._artifacts(request, root, evidence_root=evidence_root)
        plan = json.loads((evidence_root / "plan.json").read_text(encoding="utf-8"))
        timeline = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
        source_timeline = (
            json.loads((evidence_root / "timeline.json").read_text(encoding="utf-8"))
            if evidence_root != root
            else timeline
        )
        manifest = json.loads((root / "audio-manifest.json").read_text(encoding="utf-8"))
        quality = json.loads((root / "quality.json").read_text(encoding="utf-8"))
        resolved = json.loads((evidence_root / "resolved-config.json").read_text(encoding="utf-8"))
        model_metadata = json.loads((root / "model-metadata.json").read_text(encoding="utf-8"))
        if evidence_root != root:
            source_metadata = json.loads(
                (evidence_root / "model-metadata.json").read_text(encoding="utf-8")
            )
            model_metadata["llm"] = source_metadata.get("llm")
        requested_seconds = request.case.duration_seconds
        actual_seconds = float(manifest["duration_ms"]) / 1_000
        duration_error_percent = abs(actual_seconds - requested_seconds) / requested_seconds * 100
        if request.experiment_type is not None:
            candidate_resolved = json.loads(
                (root / "resolved-config.json").read_text(encoding="utf-8")
            )
            source_record = json.loads((evidence_root / "run.json").read_text(encoding="utf-8"))
            self._attest_candidate_controls(
                request,
                resolved=candidate_resolved,
                model_metadata=model_metadata,
                source_resolved=resolved,
                source_prompt=source_record.get("prompt"),
                source_plan=plan,
            )
        canonical_source: CanonicalSourceProvenance | None = None
        if request.controlled_source_run_id is not None:
            pacing_profile = self.pacing_profiles.get(request.condition.pacing_profile_id)
            allowed_mutations: set[str] = (
                set(pacing_profile.allowed_timeline_mutations)
                if request.experiment_type == "pacing"
                else {
                    "SPEECH.delivery",
                    "SPEECH.intensity",
                }
                if request.experiment_type == "delivery"
                else set()
            )
            self._attest_timeline_mutations(
                source_timeline,
                timeline,
                allowed_mutations=allowed_mutations,
            )
            if pinned_source is None:
                pinned_source, _captured = self._capture_canonical_source(
                    request.controlled_source_run_id
                )
            self._verify_canonical_source(pinned_source)
            if pinned_source.topology_sha256 != self._timeline_topology_hash(source_timeline):
                raise ValueError("Canonical source topology changed during controlled execution.")
            canonical_source = pinned_source
        if duration_error_percent > 5:
            raise ValueError(
                "Production Lab candidate is outside the requested duration gate: "
                f"error_percent={duration_error_percent:.3f}, limit=5."
            )
        metrics = [
            MetricValue(
                name="duration_error_percent",
                value=duration_error_percent,
                unit="percent",
                goal="lower",
            ),
            MetricValue(
                name="clipped_samples",
                value=float(quality["clipped_samples"]),
                unit="samples",
                goal="lower",
            ),
            MetricValue(
                name="safety_validation_passed",
                value=1,
                unit="boolean",
                goal="higher",
            ),
        ]
        if started_clock is not None:
            metrics.insert(
                0,
                MetricValue(
                    name="elapsed_seconds",
                    value=time.monotonic() - started_clock,
                    unit="seconds",
                    goal="lower",
                ),
            )
        adapters = {
            name: AdapterMetadata.model_validate(value)
            for name, value in model_metadata.items()
            if value is not None and name in {"llm", "tts"}
        }
        generation = resolved.get("generation") or {}
        # The compared writing method is recorded by ``actual_condition`` and in
        # the immutable run configuration.  It is intentionally excluded from
        # this fingerprint: a writing-method experiment must prove that the
        # prompt contract itself stayed fixed while that one condition changed.
        schema_payload = {
            "plan_schema_version": plan.get("schema_version"),
            "plan_prompt_id": generation.get("plan_prompt_id"),
            "plan_prompt_version": generation.get("plan_prompt_version"),
            "section_prompt_id": generation.get("section_prompt_id"),
            "section_prompt_version": generation.get("section_prompt_version"),
        }
        spoken = [
            segment["text"] for segment in timeline["segments"] if segment["type"] == "SPEECH"
        ]
        speech_controls = [
            {
                key: segment.get(key)
                for key in (
                    "delivery",
                    "intensity",
                    "language",
                    "voice_mode",
                    "pace",
                    "pronunciation_overrides",
                )
            }
            for segment in timeline["segments"]
            if segment["type"] == "SPEECH"
        ]
        silence = [
            {
                key: segment[key]
                for key in ("type", "duration_ms", "phase", "cycle", "protocol_id")
                if key in segment
            }
            for segment in timeline["segments"]
            if segment["type"] in {"SILENCE", "BREATH"}
        ]
        fingerprints = ControlledInputFingerprints(
            prompt_sha256=self._value_hash(request.case.prompt),
            blueprint_sha256=self._value_hash(
                {
                    "requested": request.case.blueprint_id or request.case.category,
                    "category": request.case.category,
                }
            ),
            generation_schema_sha256=self._value_hash(schema_payload),
            spoken_text_sha256=self._value_hash(spoken),
            speech_controls_sha256=self._value_hash(speech_controls),
            silence_timeline_sha256=self._value_hash(silence),
            timeline_topology_sha256=self._timeline_topology_hash(timeline),
            pacing_profile_sha256=self.pacing_profiles.get(
                request.condition.pacing_profile_id
            ).digest,
        )
        return ProductionTrialResult(
            trial_id=request.trial_id,
            source_run_id=run_id,
            controlled_source_run_id=request.controlled_source_run_id,
            candidate_id=request.candidate_id,
            case_id=request.case.id,
            success=True,
            actual_condition=request.condition,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            artifacts=artifacts,
            metrics=metrics,
            adapter_metadata=adapters,
            hardware={"system": self.application.environment_status().get("system", "unknown")},
            controlled_inputs=fingerprints,
            canonical_source=canonical_source,
        )

    def _attest_candidate_controls(
        self,
        request: ProductionTrialRequest,
        *,
        resolved: dict[str, Any],
        model_metadata: dict[str, Any],
        source_resolved: dict[str, Any],
        source_prompt: Any,
        source_plan: dict[str, Any],
    ) -> None:
        """Reject evidence whose durable production controls differ from the request."""

        condition = request.condition
        voice = condition.voice
        expected_backend = self.VOICE_BACKENDS.get(voice.model_id.casefold())
        expected_profile = self.LLM_PROFILES.get(condition.llm_model_id.casefold())
        expected_speed = self.pacing_profiles.get(condition.pacing_profile_id).speech_speed
        tts = resolved.get("tts") or {}
        mismatches: list[str] = []

        def expect(field: str, actual: Any, expected: Any) -> None:
            if actual != expected:
                mismatches.append(f"{field}={actual!r} (expected {expected!r})")

        expect("tts.backend", tts.get("backend"), expected_backend)
        expect("tts.seed", tts.get("seed"), request.seed)
        expect(
            "tts.speed", tts.get("speed"), expected_speed if expected_backend == "kokoro" else 1.0
        )
        delivery, intensity = self._effective_delivery(
            expected_backend, voice.delivery, voice.intensity
        )
        expect("tts.delivery_override", tts.get("delivery_override"), delivery)
        expect("tts.intensity_override", tts.get("intensity_override"), intensity)
        expect("tts.voice_mode", tts.get("voice_mode"), voice.voice_mode)

        if expected_backend == "kokoro":
            expect("tts.voice_name", tts.get("voice_name"), voice.voice_id)
            expect("tts.language", tts.get("language"), "en-us")
        elif expected_backend == "fish-1.4":
            expect("tts.reference_id", tts.get("reference_id"), voice.voice_id)
            expect("tts.language", tts.get("language"), "en")
        elif expected_backend is not None and expected_backend.startswith("moss-"):
            expect("tts.language", tts.get("language"), voice.language)
            expect("tts.use_reference", tts.get("use_reference"), voice.voice_mode == "reference")
            expect(
                "tts.reference_id",
                tts.get("reference_id"),
                voice.voice_id if voice.voice_mode == "reference" else None,
            )
            expect(
                "tts.instruction",
                tts.get("instruction"),
                voice.instruction or self._moss_instruction(voice.delivery, voice.intensity),
            )
        elif expected_backend is not None and expected_backend.startswith("qwen3-"):
            expect("tts.language", tts.get("language"), voice.language)
            if expected_backend.startswith("qwen3-custom-voice-"):
                expect("tts.voice_name", tts.get("voice_name"), voice.voice_id)
            elif expected_backend == "qwen3-voice-design-1.7b":
                expect("tts.voice_name", tts.get("voice_name"), voice.voice_id)
                expect(
                    "tts.qwen_voice_design_preset",
                    tts.get("qwen_voice_design_preset"),
                    voice.voice_id,
                )
            else:
                expect("tts.reference_id", tts.get("reference_id"), voice.voice_id)

        derived = request.controlled_source_run_id is not None
        expect("mode", resolved.get("mode"), "script_file" if derived else "generated_prompt")
        expect("profile", resolved.get("profile"), "basic" if derived else expected_profile)
        expect(
            "requested_duration_seconds",
            resolved.get("requested_duration_seconds"),
            request.case.duration_seconds if derived else None,
        )
        expect("source.prompt", source_prompt, request.case.prompt)
        if condition.writing_method == "recipe_whole_session":
            recipe = route_session(
                request.case.prompt,
                request.case.duration_seconds,
                meditation_kind=cast(MeditationKind, request.case.category),
                library=load_recipe_library(
                    self.application.config_directory / "session_recipes.yaml"
                ),
            ).recipe
            expect("source.plan.blueprint_id", source_plan.get("blueprint_id"), recipe.versioned_id)
            expect(
                "source.plan.blueprint_version",
                source_plan.get("blueprint_version"),
                recipe.version,
            )
        elif request.case.blueprint_id is not None:
            expect(
                "source.plan.blueprint_id",
                source_plan.get("blueprint_id"),
                request.case.blueprint_id,
            )
        source_generation = source_resolved.get("generation") or {}
        expect("generation.seed", source_generation.get("seed"), request.seed)
        expect(
            "generation.writing_method",
            source_generation.get("writing_method"),
            condition.writing_method,
        )
        expect(
            "generation.duration_seconds",
            source_generation.get("duration_seconds"),
            request.case.duration_seconds,
        )
        expect(
            "generation.meditation_kind",
            source_generation.get("meditation_kind"),
            request.case.category,
        )
        llm = model_metadata.get("llm") or {}
        model_id = str(llm.get("versioned_model_id", "")).casefold()
        markers = self.LLM_METADATA_MARKERS.get(str(expected_profile), ())
        if not markers or not any(marker in model_id for marker in markers):
            mismatches.append(
                f"llm.versioned_model_id={model_id!r} does not attest profile {expected_profile!r}"
            )
        if mismatches:
            raise ValueError(
                "Durable candidate controls do not match the Lab request: " + "; ".join(mismatches)
            )

    def _artifacts(
        self,
        request: ProductionTrialRequest,
        root: Path,
        *,
        evidence_root: Path | None = None,
    ) -> list[ArtifactReference]:
        declarations: tuple[
            tuple[
                Literal["plan", "script", "timeline", "audio", "qc", "manifest", "log"],
                str,
                str,
            ],
            ...,
        ] = (
            ("plan", "plan.json", "application/json"),
            ("script", "script.md", "text/markdown"),
            ("timeline", "timeline.json", "application/json"),
            ("audio", "narration.wav", "audio/wav"),
            ("qc", "quality.json", "application/json"),
            ("manifest", "audio-manifest.json", "application/json"),
            ("manifest", "model-metadata.json", "application/json"),
            ("manifest", "resolved-config.json", "application/json"),
            ("log", "logs.jsonl", "application/x-ndjson"),
        )
        artifacts: list[ArtifactReference] = []
        for index, (role, filename, media_type) in enumerate(declarations):
            path = (
                evidence_root / filename
                if evidence_root is not None and filename == "plan.json"
                else root / filename
            )
            if not path.is_file():
                continue
            artifacts.append(
                ArtifactReference(
                    artifact_id=f"{request.trial_id.hex}-{index:02d}",
                    role=role,
                    path=str(path),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    media_type=media_type,
                )
            )
        return artifacts

    def _canonical_source_provenance(
        self,
        source_run_id: UUID,
        timeline: dict[str, Any],
    ) -> CanonicalSourceProvenance:
        """Bind derived evidence to one exact artifact and active revision."""

        root = self.application.runs_directory / str(source_run_id)
        timeline_path = root / "timeline.json"
        revision_id = "original"
        revision_pointer = root / "revisions" / "current.json"
        if revision_pointer.is_file():
            pointer = json.loads(revision_pointer.read_text(encoding="utf-8"))
            revision_id = str(pointer["revision_id"])
        return CanonicalSourceProvenance(
            source_run_id=source_run_id,
            source_revision_id=revision_id,
            artifact_path=str(timeline_path),
            artifact_sha256=hashlib.sha256(timeline_path.read_bytes()).hexdigest(),
            topology_sha256=self._timeline_topology_hash(timeline),
        )

    def _capture_canonical_source(
        self,
        source_run_id: UUID,
    ) -> tuple[CanonicalSourceProvenance, dict[str, Any]]:
        """Read canonical bytes once and pin their revision and digest."""

        root = self.application.runs_directory / str(source_run_id)
        timeline_path = root / "timeline.json"
        timeline_bytes = timeline_path.read_bytes()
        timeline = json.loads(timeline_bytes)
        provenance = self._canonical_source_provenance(source_run_id, timeline)
        captured_digest = hashlib.sha256(timeline_bytes).hexdigest()
        if provenance.artifact_sha256 != captured_digest:
            raise ValueError("Canonical source changed while its provenance was captured.")
        return provenance, timeline

    @staticmethod
    def _verify_canonical_source(provenance: CanonicalSourceProvenance) -> None:
        path = Path(provenance.artifact_path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != provenance.artifact_sha256:
            raise ValueError("Canonical source artifact changed during controlled execution.")
        revision_pointer = path.parent / "revisions" / "current.json"
        active_revision = "original"
        if revision_pointer.is_file():
            pointer = json.loads(revision_pointer.read_text(encoding="utf-8"))
            active_revision = str(pointer["revision_id"])
        if active_revision != provenance.source_revision_id:
            raise ValueError("Canonical source revision changed during controlled execution.")

    @classmethod
    def _attest_timeline_mutations(
        cls,
        canonical: dict[str, Any],
        candidate: dict[str, Any],
        *,
        allowed_mutations: set[str],
    ) -> None:
        """Reject every canonical-timeline change outside reviewed policy."""

        canonical_segments = canonical.get("segments")
        candidate_segments = candidate.get("segments")
        if not isinstance(canonical_segments, list) or not isinstance(candidate_segments, list):
            raise ValueError("Controlled comparison timeline segments are malformed.")
        if len(canonical_segments) != len(candidate_segments):
            raise ValueError("Controlled candidate changed canonical timeline segment count.")
        for index, (source, derived) in enumerate(
            zip(canonical_segments, candidate_segments, strict=True)
        ):
            if not isinstance(source, dict) or not isinstance(derived, dict):
                raise ValueError("Controlled comparison contains a malformed timeline segment.")
            keys = set(source) | set(derived)
            for key in keys:
                if source.get(key) == derived.get(key):
                    continue
                mutation = f"{source.get('type')}.{key}"
                if mutation not in allowed_mutations:
                    raise ValueError(
                        "Controlled candidate changed non-allowlisted timeline input "
                        f"{mutation} at segment {index}."
                    )

    @classmethod
    def _timeline_topology_hash(cls, timeline: dict[str, Any]) -> str:
        """Hash ordered segment identity plus immutable breath topology."""

        topology: list[dict[str, Any]] = []
        segments = timeline.get("segments")
        if not isinstance(segments, list):
            raise ValueError("Timeline segments are malformed.")
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("Timeline contains a malformed segment.")
            item = {"id": segment.get("id"), "type": segment.get("type")}
            if segment.get("type") == "BREATH":
                item.update(
                    {
                        key: segment.get(key)
                        for key in ("duration_ms", "phase", "cycle", "protocol_id")
                    }
                )
            topology.append(item)
        return cls._value_hash(topology)

    @staticmethod
    def _value_hash(value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
