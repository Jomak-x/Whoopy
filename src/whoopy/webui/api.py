"""Versioned FastAPI control plane for Whoopy's permanent local product."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Scope

from whoopy.evaluation import (
    EvaluationExecutionError,
    EvaluationExecutionHalted,
    EvaluationLab,
    EvaluationRepository,
    EvaluationSet,
    EvaluationStorageError,
    ExperimentCandidate,
    ExperimentDefinition,
    ExperimentReport,
    HumanRating,
    ProductionExperimentExecutor,
    ProductionTrialResult,
    PromotionDecision,
    SequentialExperimentQueue,
    add_rating,
    assess_corpus_acceptance,
    blind_presentations,
    blind_review_status,
    compare_against_baseline,
    create_promotion_decision,
    load_evaluation_set,
    load_lab_acceptance_evidence,
    resolve_blind_audio,
)
from whoopy.evaluation.pacing_profiles import PacingProfileRegistry
from whoopy.meditation import REVIEWED_BREATH_PROTOCOLS, load_blueprint_library
from whoopy.pipeline.revisions import RevisionError, RevisionRequest
from whoopy.pipeline.takes import SegmentTakeError
from whoopy.voice_references import VoiceConsent
from whoopy.webui.audio_inspection import AudioInspectionError
from whoopy.webui.catalog import LocalCatalog
from whoopy.webui.evaluation import LocalProductionExperimentExecutor
from whoopy.webui.server import (
    MAX_REQUEST_BYTES,
    SEGMENT_ID_PATTERN,
    LocalWebApplication,
)

API_VERSION = 1
MAX_VOICE_REFERENCE_REQUEST_BYTES = 100 * 1024 * 1024
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
STUDIO_GENERATION_SEED = 42
STUDIO_MOSS_INSTRUCTION = "Speak slowly, softly, and warmly, with a meditative delivery."
ValueT = TypeVar("ValueT")


def _promotion_setting_keys(decision: PromotionDecision) -> tuple[str, ...]:
    """Map one immutable decision to non-conflicting rebuildable defaults."""

    if decision.outcome != "promote":
        return ()
    if decision.selection_role is None:
        return (f"studio.default.{decision.studio_target}",)
    role_key = f"studio.model_role.{decision.selection_role}"
    if decision.selection_role == "portable_default":
        return (role_key, "studio.default.llm")
    return (role_key,)


def _studio_voice_promotion_error(environment: dict[str, Any], model_id: str) -> str | None:
    """Refuse to turn Lab evidence into a default for a non-selectable voice."""

    models = environment.get("speech_models")
    model = models.get(model_id) if isinstance(models, dict) else None
    if isinstance(model, dict):
        if model.get("studio_eligible") is True:
            return None
        if (
            model_id.startswith("qwen3-")
            and model.get("ready") is True
            and model.get("studio_candidate") is True
        ):
            return None
    remedy = model.get("error") if isinstance(model, dict) else None
    detail = str(remedy) if remedy else "no current managed readiness evidence exists"
    return (
        f"{model_id} cannot become a Studio voice default because it is not currently "
        f"Studio-selectable: {detail}"
    )


def _candidate_product_value(
    experiment_type: Literal["llm", "writing_method", "voice", "delivery", "pacing"],
    candidate: ExperimentCandidate,
) -> str:
    """Derive the saved default from typed evidence, never a client-supplied label."""

    condition = candidate.condition
    if experiment_type == "llm":
        return condition.llm_model_id
    if experiment_type == "writing_method":
        return condition.writing_method
    if experiment_type == "pacing":
        return condition.pacing_profile_id
    if experiment_type == "delivery":
        return f"{condition.voice.delivery}|{condition.voice.intensity}"
    voice = condition.voice
    return "|".join(
        (
            voice.model_id,
            voice.voice_id,
            voice.voice_mode,
            voice.delivery,
            voice.intensity,
            voice.language,
        )
    )


def _supported_low_resource_llm_ids() -> tuple[str, ...]:
    """Return only model IDs that the production executor maps to Lite."""

    return tuple(
        sorted(
            model_id
            for model_id, profile in LocalProductionExperimentExecutor.LLM_PROFILES.items()
            if profile == "lite"
        )
    )


def _bundled_script_csp() -> str:
    """Authorize exact inline bootstraps from every bundled static route."""

    static_root = Path(__file__).with_name("static")
    hashes: set[str] = set()
    for html in sorted(static_root.rglob("*.html")):
        try:
            document = html.read_text(encoding="utf-8")
        except OSError:
            continue
        for source in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", document, re.DOTALL):
            digest = base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode(
                "ascii"
            )
            hashes.add(f"'sha256-{digest}'")
    return "script-src 'self'" + (" " + " ".join(sorted(hashes)) if hashes else "")


BUNDLED_SCRIPT_CSP = _bundled_script_csp()


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerationRequest(BaseModel):
    """Stable browser contract; deeper adapter validation remains authoritative."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["prompt", "script"]
    text: str = Field(min_length=1, max_length=20_000)
    minutes: float = Field(default=3, ge=1, le=30)
    tts_model: Literal[
        "kokoro",
        "fish-1.4",
        "moss-local-v1.5",
        "moss-v1.5",
        "qwen3-custom-voice-0.6b",
        "qwen3-custom-voice-1.7b",
        "qwen3-voice-design-1.7b",
        "qwen3-base-clone-0.6b",
        "qwen3-base-clone-1.7b",
    ] = "kokoro"
    surface: Literal["studio", "lab"] = "studio"
    profile: Literal["basic", "lite", "standard", "high"] | None = None
    writing_method: Literal[
        "plan_first",
        "blueprint_first_sparse",
        "three_candidate_structural_selection",
        "recipe_whole_session",
    ] = "plan_first"
    pacing_profile_id: str = Field(default="meditative-v2", pattern=r"^[a-z][a-z0-9_-]{1,95}$")
    voice: str = "af_heart"
    speed: float = Field(default=0.6, ge=0.5, le=1.2)
    meditation_kind: (
        Literal[
            "grounding",
            "focused_attention",
            "breath_awareness",
            "body_scan",
            "noting",
            "reflection",
            "loving_kindness",
            "self_compassion",
            "visualization",
            "open_awareness",
            "sleep",
        ]
        | None
    ) = None
    delivery: Literal[
        "neutral",
        "warm",
        "calm",
        "soothing",
        "grounded",
        "compassionate",
        "sleepy",
        "encouraging",
    ] = "calm"
    intensity: Literal["subtle", "moderate"] = "subtle"
    pace: Literal["natural", "unhurried", "slow", "very_slow"] = "slow"
    seed: int = Field(default=42, ge=0)
    moss_language: str | None = None
    moss_instruction: str | None = Field(default=None, max_length=300)
    moss_use_reference: bool | None = None
    voice_reference: str | None = None
    qwen_language: str | None = None
    qwen_speaker: str | None = None
    qwen_voice_design_preset: (
        Literal["warm_meditation", "grounded_meditation", "sleep_meditation"] | None
    ) = None
    qwen_reference: str | None = None

    @model_validator(mode="after")
    def validate_model_controls(self) -> GenerationRequest:
        if self.mode == "prompt" and self.profile == "basic":
            raise ValueError("Prompt generation requires the Lite or Standard profile")
        if self.mode == "script" and self.profile not in {None, "basic"}:
            raise ValueError("Authored scripts use the Basic profile")
        moss_fields = (
            self.moss_language,
            self.moss_instruction,
            self.moss_use_reference,
        )
        qwen_fields = (
            self.qwen_language,
            self.qwen_speaker,
            self.qwen_voice_design_preset,
            self.qwen_reference,
        )
        if not self.tts_model.startswith("moss-") and any(
            value is not None for value in moss_fields
        ):
            raise ValueError("MOSS controls are valid only for MOSS speech models")
        if self.voice_reference is not None and self.tts_model not in {
            "fish-1.4",
            "moss-local-v1.5",
            "moss-v1.5",
        }:
            raise ValueError(
                "voice_reference is valid only for Fish or MOSS; Qwen Base uses qwen_reference"
            )
        if (
            self.voice_reference is not None
            and self.tts_model.startswith("moss-")
            and self.moss_use_reference is False
        ):
            raise ValueError("MOSS direct voice mode cannot use a voice reference")
        if not self.tts_model.startswith("qwen3-") and any(
            value is not None for value in qwen_fields
        ):
            raise ValueError("Qwen controls are valid only for Qwen3-TTS speech models")
        if self.tts_model.startswith("qwen3-custom-voice-"):
            if self.qwen_reference is not None or self.qwen_voice_design_preset is not None:
                raise ValueError("Qwen CustomVoice accepts only a preset speaker")
        elif self.tts_model == "qwen3-voice-design-1.7b":
            if self.qwen_reference is not None or self.qwen_speaker is not None:
                raise ValueError("Qwen VoiceDesign accepts only a reviewed design preset")
        elif self.tts_model.startswith("qwen3-base-clone-"):
            if not self.qwen_reference:
                raise ValueError("Qwen Base cloning requires a consented voice reference")
            if self.qwen_speaker is not None or self.qwen_voice_design_preset is not None:
                raise ValueError("Qwen Base cloning accepts only a consented voice reference")
        if self.tts_model in {
            "qwen3-custom-voice-0.6b",
            "qwen3-base-clone-0.6b",
            "qwen3-base-clone-1.7b",
        } and (self.delivery, self.intensity, self.pace) != ("neutral", "subtle", "natural"):
            raise ValueError(
                f"{self.tts_model} supports only neutral delivery, subtle intensity, "
                "and natural pace"
            )
        if self.surface == "studio" and self.seed != STUDIO_GENERATION_SEED:
            raise ValueError("Custom generation seeds are Lab-only")
        if (
            self.surface == "studio"
            and self.moss_instruction is not None
            and self.moss_instruction != STUDIO_MOSS_INSTRUCTION
        ):
            raise ValueError("Free-form MOSS instructions are Lab-only")
        return self


class ModelPackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_network: bool | None = None
    confirm: bool | None = None
    receipt_id: str | None = None


class SettingsImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    settings: dict[str, Any]


class ExperimentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)


class HumanRatingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: UUID
    dimension: Literal["content", "voice", "pacing", "naturalness", "safety", "preference"]
    score: int = Field(ge=1, le=5)
    preferred_side: Literal["left", "right", "tie"] | None = None
    notes: str = Field(default="", max_length=4_000)


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["promote", "reject"]
    selection_role: (
        Literal["portable_default", "low_resource_fallback", "higher_quality"] | None
    ) = None
    candidate_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    rationale: str = Field(min_length=1, max_length=4_000)


class _LabApiService:
    """Local FIFO worker backed exclusively by ordinary production runs."""

    def __init__(
        self,
        *,
        application: LocalWebApplication,
        executor: ProductionExperimentExecutor | None,
    ) -> None:
        self.application = application
        self.executor = executor
        self.repository = EvaluationRepository(application.runs_directory / ".whoopy-lab")
        self.queue = SequentialExperimentQueue()
        self._definitions: dict[tuple[str, int], ExperimentDefinition] = {}
        self._case_selections: dict[tuple[str, int], tuple[str, ...] | None] = {}
        self._controls: dict[tuple[str, int], dict[str, Any]] = {}
        self._control_lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        for definition in self.repository.definitions():
            key = (definition.experiment_id, definition.version)
            self._definitions[key] = definition
            control = self.repository.load_execution_control(
                experiment_id=definition.experiment_id,
                version=definition.version,
            )
            if control is not None:
                if control.get("status") in {"queued", "running"}:
                    control["status"] = "paused"
                    control["halt_action"] = None
                    control["active_run_id"] = None
                    control["updated_at"] = datetime.now(UTC).isoformat()
                    self.repository.save_execution_control(
                        experiment_id=definition.experiment_id,
                        version=definition.version,
                        payload=control,
                    )
                self._controls[key] = control
        if isinstance(self.executor, LocalProductionExperimentExecutor):
            self.executor.active_run_changed = self._active_run_changed

    @staticmethod
    def _new_control(
        definition: ExperimentDefinition, case_ids: tuple[str, ...] | None
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        return {
            "schema_version": 1,
            "experiment_id": definition.experiment_id,
            "experiment_version": definition.version,
            "status": "queued",
            "requested_case_ids": None if case_ids is None else list(case_ids),
            "trials": [],
            "resumable_runs": {},
            "active_run_id": None,
            "halt_action": None,
            "failure": None,
            "created_at": now,
            "updated_at": now,
        }

    def _save_control(self, key: tuple[str, int], control: dict[str, Any]) -> None:
        control["updated_at"] = datetime.now(UTC).isoformat()
        self.repository.save_execution_control(
            experiment_id=key[0], version=key[1], payload=control
        )
        self._controls[key] = control

    def _active_run_changed(self, run_id: UUID | None) -> None:
        with self._control_lock:
            for key, control in self._controls.items():
                if control.get("status") == "running":
                    control["active_run_id"] = None if run_id is None else str(run_id)
                    self._save_control(key, control)
                    return

    def capabilities(self) -> dict[str, Any]:
        provider = getattr(self.executor, "capabilities", None)
        if callable(provider):
            return dict(provider(execution_available=self.executor is not None))
        return LocalProductionExperimentExecutor.capabilities(
            execution_available=self.executor is not None
        )

    def _capability_error(self, definition: ExperimentDefinition) -> str | None:
        validator = getattr(self.executor, "validate_definition", None)
        if not callable(validator):
            return None
        try:
            validator(definition)
        except EvaluationExecutionError as error:
            return str(error)
        return None

    def _require_capable(self, definition: ExperimentDefinition) -> None:
        error = self._capability_error(definition)
        if error is not None:
            raise EvaluationExecutionError(error)

    def evaluation_sets(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        root = self.application.config_directory / "evaluation"
        for path in sorted(root.glob("*.yaml")):
            try:
                evaluation = load_evaluation_set(path)
            except ValueError:
                continue
            values.append(
                {
                    "evaluation_id": evaluation.evaluation_id,
                    "version": evaluation.version,
                    "schema_version": evaluation.schema_version,
                    "case_count": len(evaluation.cases),
                    "cases": [case.model_dump(mode="json") for case in evaluation.cases],
                }
            )
        return values

    def evaluation_set_by_id(self, evaluation_id: str, version: int) -> EvaluationSet:
        root = self.application.config_directory / "evaluation"
        for path in sorted(root.glob("*.yaml")):
            try:
                evaluation = load_evaluation_set(path)
            except ValueError:
                continue
            if evaluation.evaluation_id == evaluation_id and evaluation.version == version:
                return evaluation
        raise EvaluationExecutionError("Referenced versioned evaluation set is not installed.")

    def add_definition(self, definition: ExperimentDefinition) -> dict[str, Any]:
        self._require_capable(definition)
        self.repository.save_definition(definition)
        self._definitions[(definition.experiment_id, definition.version)] = definition
        return {
            "definition": definition.model_dump(mode="json"),
            "queue": None,
            "execution_available": self.executor is not None,
            "next_action": (
                "POST the execute endpoint to start the production pipeline."
                if self.executor is not None
                else (
                    "Configure a production evaluation executor; "
                    "no Lab-only output will be created."
                )
            ),
        }

    def experiments(self) -> dict[str, Any]:
        statuses = {
            (entry.experiment_id, entry.experiment_version): entry for entry in self.queue.entries()
        }
        values: list[dict[str, Any]] = []
        for key, definition in sorted(self._definitions.items()):
            entry = statuses.get(key)
            report_available = self.repository.has_report(
                experiment_id=definition.experiment_id,
                version=definition.version,
            )
            report_summary: dict[str, Any] | None = None
            if report_available:
                report = self.repository.load_report(
                    experiment_id=definition.experiment_id,
                    version=definition.version,
                )
                report_summary = {
                    "comparison_status": report.comparison_status,
                    "incomplete_candidates_by_case": report.incomplete_candidates_by_case,
                }
            values.append(
                {
                    "definition": definition.model_dump(mode="json"),
                    "queue": None if entry is None else entry.model_dump(mode="json"),
                    "report_available": report_available,
                    "report_summary": report_summary,
                    "capability_error": self._capability_error(definition),
                    "execution": self._controls.get(key),
                }
            )
        return {"experiments": values, "execution_available": self.executor is not None}

    def execute(
        self,
        *,
        experiment_id: str,
        version: int,
        case_ids: list[str] | None,
    ) -> dict[str, Any]:
        requested = self.definition(experiment_id, version)
        self._require_capable(requested)
        if self.executor is None:
            raise EvaluationExecutionError(
                "Experiment remains queued: no production evaluation executor is configured. "
                "Configure the normal Whoopy generation pipeline before executing Lab trials."
            )
        selected = None if case_ids is None else tuple(case_ids)
        with self._worker_lock:
            key = (requested.experiment_id, requested.version)
            if self.repository.has_report(experiment_id=key[0], version=key[1]):
                raise EvaluationExecutionError(
                    "Experiment report is already final; create a new revision to rerun."
                )
            current_entry = next(
                (
                    entry
                    for entry in self.queue.entries()
                    if (entry.experiment_id, entry.experiment_version) == key
                ),
                None,
            )
            control = self._controls.get(key)
            if control is None:
                control = self._new_control(requested, selected)
            else:
                # Pausing at the boundary before trial one is deliberately recorded as a
                # failed queue attempt ("batch paused") while the durable execution remains
                # resumable.  A resume racing that finalization used to persist ``queued``
                # before rejecting the terminal queue entry, permanently wedging the revision.
                # Reconcile that exact state before validating or mutating the next attempt.
                paused_boundary = (
                    control.get("status") == "queued"
                    and current_entry is not None
                    and current_entry.status == "failed"
                    and current_entry.failure == "batch paused"
                    and control.get("active_run_id") is None
                )
                if paused_boundary:
                    control["status"] = "paused"
                if control.get("status") not in {"paused", "cancelled", "incomplete"}:
                    raise EvaluationExecutionError(
                        "Experiment revision is already queued or running."
                    )
                stored_cases = control.get("requested_case_ids")
                if case_ids is not None and stored_cases != list(selected or ()):
                    raise EvaluationExecutionError(
                        "Resume must use the original case selection; create a new revision "
                        "to change it."
                    )
                control["status"] = "queued"
                control["halt_action"] = None
                control["failure"] = None
            if current_entry is None or current_entry.status in {"failed", "cancelled"}:
                self.queue.enqueue(requested)
            elif current_entry.status not in {"queued", "running"}:
                raise EvaluationExecutionError(
                    "Experiment is already complete or cancelled; create a new revision to rerun."
                )
            self._case_selections[key] = selected
            self._save_control(key, control)
            if self._worker is not None and self._worker.is_alive():
                return {
                    "accepted": True,
                    "status": "queued",
                    "message": "A production experiment is running; this revision remains queued.",
                }
            entry = self.queue.start_next()
            if entry is None:
                raise EvaluationExecutionError("Experiment is not queued or is already complete.")
            first = self.definition(entry.experiment_id, entry.experiment_version)
            self._worker = threading.Thread(
                target=self._drain_queue,
                args=(first,),
                daemon=True,
                name="whoopy-evaluation-worker",
            )
            self._worker.start()
        return {
            "accepted": True,
            "status": "running",
            "experiment_id": entry.experiment_id,
            "experiment_version": entry.experiment_version,
        }

    def halt(
        self, *, experiment_id: str, version: int, action: Literal["pause", "cancel"]
    ) -> dict[str, Any]:
        key = (experiment_id, version)
        self.definition(experiment_id, version)
        with self._control_lock:
            control = self._controls.get(key)
            if control is None or control.get("status") not in {"queued", "running"}:
                raise EvaluationExecutionError(
                    "Only a queued or running experiment can be paused or cancelled."
                )
            target = "paused" if action == "pause" else "cancelled"
            control["halt_action"] = action
            active_run_id = control.get("active_run_id")
            if control.get("status") == "queued":
                with suppress(EvaluationExecutionError):
                    self.queue.cancel(experiment_id=experiment_id, experiment_version=version)
                control["status"] = target
                control["halt_action"] = None
            self._save_control(key, control)
        if isinstance(active_run_id, str):
            self.application.cancel_task(active_run_id)
        return {"accepted": True, "status": target, "active_run_id": active_run_id}

    def _drain_queue(
        self,
        definition: ExperimentDefinition,
    ) -> None:
        current = definition
        while True:
            try:
                assert self.executor is not None
                selected = self._case_selections.pop(
                    (current.experiment_id, current.version),
                    None,
                )
                key = (current.experiment_id, current.version)
                with self._control_lock:
                    control = self._controls[key]
                    control["status"] = "running"
                    self._save_control(key, control)
                if selected is None and control.get("requested_case_ids") is not None:
                    selected = tuple(control["requested_case_ids"])
                existing_trials = tuple(
                    ProductionTrialResult.model_validate(value)
                    for value in control.get("trials", [])
                )
                resumable_runs = {
                    UUID(trial_id): UUID(run_id)
                    for trial_id, run_id in control.get("resumable_runs", {}).items()
                }

                def should_halt(control_key: tuple[str, int] = key) -> bool:
                    with self._control_lock:
                        return self._controls[control_key].get("halt_action") in {
                            "pause",
                            "cancel",
                        }

                def trial_completed(
                    trials: tuple[ProductionTrialResult, ...],
                    control_key: tuple[str, int] = key,
                ) -> None:
                    with self._control_lock:
                        current_control = self._controls[control_key]
                        current_control["trials"] = [
                            trial.model_dump(mode="json") for trial in trials
                        ]
                        last = trials[-1]
                        run = self.application.run(str(last.source_run_id))
                        if run is not None and run.get("status") == "interrupted":
                            current_control.setdefault("resumable_runs", {})[str(last.trial_id)] = (
                                str(last.source_run_id)
                            )
                        self._save_control(control_key, current_control)

                environment = self.application.environment_status()
                report = EvaluationLab(
                    evaluation_set=self._evaluation_set(current),
                    platform=str(environment.get("system", "unknown-local")),
                ).run(
                    current,
                    self.executor,
                    case_ids=selected,
                    existing_trials=existing_trials,
                    resumable_runs=resumable_runs,
                    should_halt=should_halt,
                    trial_completed=trial_completed,
                )
                self.repository.save_report(report)
                with self._control_lock:
                    control = self._controls[key]
                    control["status"] = "completed"
                    control["active_run_id"] = None
                    control["trials"] = [trial.model_dump(mode="json") for trial in report.trials]
                    self._save_control(key, control)
                self.queue.finish(
                    experiment_id=current.experiment_id,
                    experiment_version=current.version,
                )
            except EvaluationExecutionHalted as halted:
                with self._control_lock:
                    control = self._controls[key]
                    control["trials"] = [trial.model_dump(mode="json") for trial in halted.trials]
                    action = control.get("halt_action")
                    control["status"] = "cancelled" if action == "cancel" else "paused"
                    control["halt_action"] = None
                    control["active_run_id"] = None
                    self._save_control(key, control)
                self.queue.finish(
                    experiment_id=current.experiment_id,
                    experiment_version=current.version,
                    failure=f"batch {control['status']}",
                )
            except Exception as error:
                with self._control_lock:
                    if "key" in locals() and key in self._controls:
                        control = self._controls[key]
                        control["status"] = "incomplete"
                        control["active_run_id"] = None
                        control["failure"] = f"{type(error).__name__}: {error}"[:2_000]
                        self._save_control(key, control)
                self.queue.finish(
                    experiment_id=current.experiment_id,
                    experiment_version=current.version,
                    failure=f"{type(error).__name__}: {error}"[:2_000],
                )
            entry = self.queue.start_next()
            if entry is None:
                return
            current = self.definition(entry.experiment_id, entry.experiment_version)

    def _evaluation_set(self, definition: ExperimentDefinition) -> EvaluationSet:
        return self.evaluation_set_by_id(
            definition.evaluation_id,
            definition.evaluation_version,
        )

    def definition(self, experiment_id: str, version: int) -> ExperimentDefinition:
        value = self._definitions.get((experiment_id, version))
        if value is None:
            raise KeyError("Experiment revision not found.")
        return value

    def report(self, experiment_id: str, version: int) -> ExperimentReport:
        report = self.repository.load_report(experiment_id=experiment_id, version=version)
        for rating in self.repository.ratings(
            experiment_id=experiment_id,
            experiment_version=version,
        ):
            report = add_rating(report, rating)
        return report


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    """Enforce the loopback browser boundary and bounded request bodies."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        voice_upload = (
            request.method == "POST" and request.url.path == "/api/v1/voice-references"
        ) or (
            request.method == "PUT"
            and re.fullmatch(r"/api/v1/voice-references/[^/]+", request.url.path) is not None
        )
        request_limit = MAX_VOICE_REFERENCE_REQUEST_BYTES if voice_upload else MAX_REQUEST_BYTES
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
                if declared_length < 0:
                    raise ValueError
                if declared_length > request_limit:
                    return JSONResponse(
                        {"error": f"Request must contain at most {request_limit} bytes."},
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    )
            except ValueError:
                return JSONResponse({"error": "Invalid Content-Length."}, status_code=400)
        if request.method in MUTATING_METHODS and not voice_upload:
            # Content-Length is optional and can be dishonest. Reading ordinary JSON/control
            # bodies here enforces the real byte count for HTTP/1.1 chunked requests too. Voice
            # WAV uploads keep their streaming path, whose writer independently enforces 100 MiB.
            body = await request.body()
            if len(body) > request_limit:
                return JSONResponse(
                    {"error": f"Request must contain at most {request_limit} bytes."},
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
        if request.method in MUTATING_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                parsed = urlparse(origin)
                request_host = request.headers.get("host", "").lower()
                valid = (
                    parsed.scheme == "http"
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.hostname in {"127.0.0.1", "localhost"}
                    and parsed.netloc.lower() == request_host
                )
                if not valid:
                    return JSONResponse(
                        {"error": "Only the local Whoopy page may change local data."},
                        status_code=status.HTTP_403_FORBIDDEN,
                    )
        response = await call_next(request)
        if "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            f"style-src 'self' 'unsafe-inline'; {BUNDLED_SCRIPT_CSP}"
        )
        return response


class ProductStaticFiles(StaticFiles):
    """Serve rebuildable HTML without leaving clients on deleted asset hashes.

    Svelte's fingerprinted assets are safe to cache indefinitely, but the HTML
    shell names those hashes and must be revalidated on every local app load.
    Without an explicit policy, a browser can reuse an older shell after a
    Whoopy upgrade and show a blank screen because its referenced bundle was
    correctly replaced.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        elif "/_app/immutable/" in str(scope.get("path", "")):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _not_found(value: ValueT | None, detail: str) -> ValueT:
    if value is None:
        raise HTTPException(status_code=404, detail=detail)
    return value


def _voice_consent(
    statement: str,
    source_kind: Literal["user_provided", "project_generated"],
) -> VoiceConsent:
    expected: Literal["I own this voice or have permission to clone it."] = (
        "I own this voice or have permission to clone it."
    )
    if statement != expected:
        raise ValueError(f"X-Whoopy-Voice-Consent must exactly state: {expected}")
    return VoiceConsent(
        confirmed=True,
        scope="local_voice_cloning_experiment_only",
        source_kind=source_kind,
        rights_attestation=expected,
        recorded_at=datetime.now(UTC),
    )


async def _temporary_wav(request: Request) -> Path:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type not in {"audio/wav", "audio/x-wav", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Voice reference body must be a WAV file.")
    descriptor, name = tempfile.mkstemp(prefix="whoopy-voice-reference-", suffix=".wav")
    path = Path(name)
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as destination:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_VOICE_REFERENCE_REQUEST_BYTES:
                    raise HTTPException(status_code=413, detail="Voice-reference WAV is too large.")
                destination.write(chunk)
        if total == 0:
            raise ValueError("Voice-reference WAV body cannot be empty.")
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def create_api(application: LocalWebApplication) -> FastAPI:
    """Compose the versioned API around the existing production service."""

    catalog = LocalCatalog(application.runs_directory / ".whoopy-catalog.sqlite3")
    lab = _LabApiService(
        application=application,
        executor=application.evaluation_executor or LocalProductionExperimentExecutor(application),
    )

    def refresh_lab_catalog() -> None:
        """Rebuild disposable Lab rows from immutable repository JSON."""

        indexed_at = datetime.now(UTC).isoformat()
        experiment_rows: list[dict[str, Any]] = []
        rating_rows: list[dict[str, Any]] = []
        for definition in lab.repository.definitions():
            root = (
                lab.repository.root
                / "experiments"
                / definition.experiment_id
                / f"v{definition.version}"
            )
            candidate_report_path = root / "report.json"
            report_path: Path | None
            projection_status: str
            if candidate_report_path.is_file():
                report_path = candidate_report_path
                report = lab.repository.load_report(
                    experiment_id=definition.experiment_id,
                    version=definition.version,
                )
                projection_status = report.comparison_status
                projected_ratings = {str(value.rating_id): value for value in report.ratings}
            else:
                report_path = None
                projection_status = "defined"
                projected_ratings = {}
            experiment_rows.append(
                {
                    "experiment_id": definition.experiment_id,
                    "experiment_version": definition.version,
                    "kind": definition.experiment_type,
                    "status": projection_status,
                    "definition": definition.model_dump(mode="json"),
                    "definition_path": str(root / "definition.json"),
                    "report_path": None if report_path is None else str(report_path),
                    "updated_at": indexed_at,
                }
            )
            projected_ratings.update(
                {
                    str(value.rating_id): value
                    for value in lab.repository.ratings(
                        experiment_id=definition.experiment_id,
                        experiment_version=definition.version,
                    )
                }
            )
            for rating in projected_ratings.values():
                rating_rows.append(
                    {
                        "rating_id": str(rating.rating_id),
                        "experiment_id": definition.experiment_id,
                        "experiment_version": definition.version,
                        "assignment_id": str(rating.assignment_id),
                        "dimension": rating.dimension,
                        "score": rating.score,
                        "preferred_side": rating.preferred_side,
                        "notes": rating.notes,
                        "created_at": rating.created_at.isoformat(),
                    }
                )
        decision_rows = [
            {
                "decision_id": str(decision.decision_id),
                "experiment_id": decision.experiment_id,
                "experiment_version": decision.experiment_version,
                "outcome": decision.outcome,
                "target": decision.studio_target,
                "selection_role": decision.selection_role,
                "candidate_id": decision.candidate_id,
                "selected_value": decision.selected_value,
                "rationale": decision.rationale,
                "created_at": decision.created_at.isoformat(),
            }
            for decision in lab.repository.decisions()
        ]
        catalog.replace_lab_projection(
            experiments=experiment_rows,
            ratings=rating_rows,
            decisions=decision_rows,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        application.reconcile_runs()
        catalog.replace_runs(application.list_runs())
        refresh_lab_catalog()
        yield

    app = FastAPI(
        title="Whoopy Local API",
        version=str(API_VERSION),
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.add_middleware(LocalSecurityMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        details = error.errors()
        for detail in details:
            context = detail.get("ctx")
            if isinstance(context, dict):
                detail["ctx"] = {
                    key: str(value) if isinstance(value, BaseException) else value
                    for key, value in context.items()
                }
        return JSONResponse(
            {"error": "The request is invalid.", "details": details},
            status_code=422,
        )

    @app.exception_handler(ValueError)
    async def value_error(_request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=400)

    @app.exception_handler(EvaluationExecutionError)
    async def evaluation_execution_error(
        _request: Request,
        error: EvaluationExecutionError,
    ) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=409)

    @app.exception_handler(EvaluationStorageError)
    async def evaluation_storage_error(
        _request: Request,
        error: EvaluationStorageError,
    ) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=409)

    @app.exception_handler(SegmentTakeError)
    @app.exception_handler(RevisionError)
    @app.exception_handler(AudioInspectionError)
    async def editing_conflict(_request: Request, error: RuntimeError) -> JSONResponse:
        return JSONResponse({"error": str(error)}, status_code=409)

    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "whoopy-local",
            "api_version": API_VERSION,
            "surfaces": ["studio", "lab"],
            "authoritative_store": str(application.runs_directory),
        }

    @router.get("/status")
    def environment_status() -> dict[str, Any]:
        return application.environment_status()

    @router.get("/speech-models")
    def speech_models() -> dict[str, Any]:
        """Return readiness beside the exact typed controls each adapter consumes."""

        environment = application.environment_status()
        return {
            "ok": environment.get("ok", False),
            "profiles": environment.get("profiles", {}),
            "speech_models": environment.get("speech_models", {}),
            "error": environment.get("error"),
        }

    @router.get("/model-packs")
    def model_packs() -> dict[str, Any]:
        return application.list_model_packs()

    @router.get("/runtime-packs")
    def runtime_packs() -> dict[str, Any]:
        """List immutable isolated runtimes and their exact current-machine remedy."""

        return application.list_runtime_packs()

    @router.post("/runtime-pack-tasks/{runtime_id}/install", status_code=202)
    def start_runtime_pack_task(
        runtime_id: str,
        body: ModelPackRequest,
    ) -> dict[str, Any]:
        extra = set(body.model_dump(exclude_none=True)) - {"allow_network"}
        if extra:
            raise ValueError("Runtime install accepts only allow_network.")
        return application.start_runtime_pack_task(
            "install", runtime_id, {"allow_network": bool(body.allow_network)}
        )

    @router.get("/runtime-pack-tasks/{task_id}")
    def runtime_pack_task(task_id: UUID) -> dict[str, Any]:
        return _not_found(application.runtime_pack_task(str(task_id)), "Runtime task not found.")

    @router.post("/runtime-packs/{runtime_id}/verify")
    def verify_runtime_pack(runtime_id: str, _body: EmptyBody) -> dict[str, Any]:
        return application.runtime_pack_action("verify", runtime_id, {})

    @router.get("/model-packs/{pack_id}/sample", response_class=FileResponse)
    def model_pack_sample(pack_id: str) -> FileResponse:
        path = application.model_pack_sample_path(pack_id)
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=f"{pack_id}-smoke-sample.wav",
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/model-pack-tasks/{pack_id}/{action}", status_code=202)
    def start_model_pack_task(
        pack_id: str,
        action: str,
        body: ModelPackRequest,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if action == "install":
            payload["allow_network"] = bool(body.allow_network)
        elif body.model_dump(exclude_none=True):
            raise ValueError(f"{action} does not accept request fields.")
        return application.start_model_pack_task(action, pack_id, payload)

    @router.get("/model-pack-tasks/{task_id}")
    def model_pack_task(task_id: UUID) -> dict[str, Any]:
        return _not_found(application.model_pack_task(str(task_id)), "Model task not found.")

    @router.get("/voice-references")
    def voice_references() -> dict[str, Any]:
        return application.list_voice_references()

    @router.get("/voice-demo-reference")
    def voice_demo_reference() -> dict[str, Any]:
        return application.demo_voice_reference()

    @router.get("/voice-demo-reference/preview", response_class=FileResponse)
    def voice_demo_reference_preview() -> FileResponse:
        path, duration_ms = application.demo_voice_reference_preview_path()
        return FileResponse(
            path,
            media_type="audio/wav",
            filename="whoopy-local-demo-reference.wav",
            headers={"X-Whoopy-Preview-Duration-Ms": str(duration_ms)},
        )

    @router.post("/voice-references", status_code=201)
    async def import_voice_reference(
        request: Request,
        reference_id: str = Query(min_length=1, max_length=128),
        display_name: str = Query(min_length=1, max_length=120),
        transcript: str = Query(min_length=1, max_length=10_000),
        source_kind: Literal["user_provided", "project_generated"] = Query(),
        trim_start_ms: int = Query(default=0, ge=0, le=59_000),
        trim_end_ms: int | None = Query(default=None, ge=1, le=60_000),
        consent_statement: str = Header(alias="X-Whoopy-Voice-Consent"),
    ) -> dict[str, Any]:
        temporary = await _temporary_wav(request)
        try:
            return application.import_voice_reference(
                reference_id=reference_id,
                display_name=display_name,
                source_wav=temporary,
                transcript=transcript,
                consent=_voice_consent(consent_statement, source_kind),
                trim_start_ms=trim_start_ms,
                trim_end_ms=trim_end_ms,
                replace=False,
            )
        finally:
            temporary.unlink(missing_ok=True)

    @router.get("/voice-references/{reference_id}")
    def voice_reference(reference_id: str) -> dict[str, Any]:
        return application.voice_reference(reference_id)

    @router.get("/voice-references/{reference_id}/preview", response_class=FileResponse)
    def voice_reference_preview(reference_id: str) -> FileResponse:
        path, duration_ms = application.voice_reference_preview_path(reference_id)
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=f"{reference_id}-preview.wav",
            headers={"X-Whoopy-Preview-Duration-Ms": str(duration_ms)},
        )

    @router.put("/voice-references/{reference_id}", status_code=201)
    async def replace_voice_reference(
        reference_id: str,
        request: Request,
        display_name: str = Query(min_length=1, max_length=120),
        transcript: str = Query(min_length=1, max_length=10_000),
        source_kind: Literal["user_provided", "project_generated"] = Query(),
        trim_start_ms: int = Query(default=0, ge=0, le=59_000),
        trim_end_ms: int | None = Query(default=None, ge=1, le=60_000),
        consent_statement: str = Header(alias="X-Whoopy-Voice-Consent"),
    ) -> dict[str, Any]:
        temporary = await _temporary_wav(request)
        try:
            return application.import_voice_reference(
                reference_id=reference_id,
                display_name=display_name,
                source_wav=temporary,
                transcript=transcript,
                consent=_voice_consent(consent_statement, source_kind),
                trim_start_ms=trim_start_ms,
                trim_end_ms=trim_end_ms,
                replace=True,
            )
        finally:
            temporary.unlink(missing_ok=True)

    @router.delete("/voice-references/{reference_id}")
    def delete_voice_reference(
        reference_id: str,
        confirmation: str = Query(min_length=1, max_length=180),
    ) -> dict[str, Any]:
        return application.delete_voice_reference(reference_id, confirmation=confirmation)

    @router.post("/voice-references/trash/{trash_id}/restore")
    def restore_voice_reference(
        trash_id: str,
        confirmation: str = Query(min_length=1, max_length=200),
    ) -> dict[str, Any]:
        return application.restore_voice_reference(trash_id, confirmation=confirmation)

    @router.post("/model-packs/unload")
    def unload_model(_body: EmptyBody) -> dict[str, Any]:
        return application.model_pack_action("unload", None, {})

    @router.post("/model-packs/restore")
    def restore_model(body: ModelPackRequest) -> dict[str, Any]:
        if body.receipt_id is None:
            raise ValueError("Restore requires a receipt_id.")
        return application.model_pack_action("restore", None, {"receipt_id": body.receipt_id})

    @router.post("/model-packs/{pack_id}/{action}")
    def change_model_pack(pack_id: str, action: str, body: ModelPackRequest) -> dict[str, Any]:
        if action not in {"install", "verify", "smoke-test", "remove"}:
            raise HTTPException(status_code=404, detail="Model-pack action not found.")
        payload: dict[str, Any] = {}
        if action == "install":
            payload["allow_network"] = bool(body.allow_network)
        elif action == "remove":
            payload["confirm"] = bool(body.confirm)
        elif body.model_dump(exclude_none=True):
            raise ValueError(f"{action} does not accept request fields.")
        return application.model_pack_action(action, pack_id, payload)

    @router.get("/runs")
    def runs(
        query: str = Query(default="", max_length=200),
        audience: Literal["studio", "lab", "system", "all"] = "studio",
        run_status: str | None = Query(default=None, alias="status", max_length=30),
        tts_model: str | None = Query(default=None, max_length=100),
        minimum_duration_seconds: float | None = Query(default=None, ge=0, le=86_400),
        maximum_duration_seconds: float | None = Query(default=None, ge=0, le=86_400),
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        if created_from is not None and created_from.tzinfo is None:
            raise ValueError("Creation start must include a timezone.")
        if created_to is not None and created_to.tzinfo is None:
            raise ValueError("Creation end must include a timezone.")
        if (
            minimum_duration_seconds is not None
            and maximum_duration_seconds is not None
            and minimum_duration_seconds > maximum_duration_seconds
        ):
            raise ValueError("Minimum duration cannot exceed maximum duration.")
        if created_from is not None and created_to is not None and created_from > created_to:
            raise ValueError("Creation start cannot be after creation end.")
        catalog.replace_runs(application.list_runs())
        return {
            "runs": catalog.search_runs(
                query=query,
                audience=None if audience == "all" else audience,
                status=run_status,
                tts_model=tts_model,
                minimum_duration_seconds=minimum_duration_seconds,
                maximum_duration_seconds=maximum_duration_seconds,
                created_from=created_from,
                created_to=created_to,
                limit=limit,
            )
        }

    @router.get("/trash/runs")
    def deleted_runs() -> dict[str, Any]:
        values = [
            {key: value for key, value in item.items() if key != "path"}
            for item in application.list_deleted_runs()
        ]
        return {"runs": values}

    @router.post("/trash/runs/{receipt_id}/restore")
    def restore_deleted_run(
        receipt_id: str,
        confirmation: str = Query(min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = _not_found(
            application.restore_deleted_run(receipt_id, confirmation=confirmation),
            "Deleted meditation not found or its receipt is no longer intact.",
        )
        catalog.replace_runs(application.list_runs())
        return result

    @router.get("/runs/{run_id}")
    def run(run_id: UUID) -> dict[str, Any]:
        return _not_found(application.run(str(run_id)), "Run not found.")

    @router.get("/runs/{run_id}/generation-attempts")
    def generation_attempts(run_id: UUID) -> dict[str, Any]:
        attempts = _not_found(
            application.generation_attempts(str(run_id)),
            "Run not found.",
        )
        return {"run_id": str(run_id), "attempts": attempts}

    @router.get("/tasks/{task_id}")
    def task(task_id: UUID) -> dict[str, Any]:
        return _not_found(application.task(str(task_id)), "Task not found.")

    @router.get("/runs/{run_id}/audio", response_class=FileResponse)
    def audio(run_id: UUID) -> FileResponse:
        path = _not_found(application.audio_path(str(run_id)), "Audio not found.")
        return FileResponse(path, media_type="audio/wav", filename="meditation.wav")

    @router.get("/runs/{run_id}/waveform")
    def waveform(
        run_id: UUID,
        points: int = Query(default=900, ge=100, le=2_000),
    ) -> dict[str, Any]:
        return _not_found(
            application.waveform(str(run_id), points=points),
            "Finished audio not found.",
        )

    @router.get("/runs/{run_id}/segments/{segment_id}/audition")
    def segment_audition(run_id: UUID, segment_id: str) -> Response:
        if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
            raise ValueError("Invalid segment ID.")
        payload = _not_found(
            application.segment_audition(str(run_id), segment_id),
            "Segment audio not found.",
        )
        return Response(
            content=payload,
            media_type="audio/wav",
            headers={"Content-Disposition": f'inline; filename="{segment_id}.wav"'},
        )

    @router.get("/runs/{run_id}/sections")
    def sections(run_id: UUID) -> dict[str, Any]:
        values = _not_found(application.sections(str(run_id)), "Run not found.")
        return {"sections": values}

    @router.get("/runs/{run_id}/sections/{section_id}/audition")
    def section_audition(run_id: UUID, section_id: str) -> Response:
        if not SEGMENT_ID_PATTERN.fullmatch(section_id):
            raise ValueError("Invalid section ID.")
        payload = _not_found(
            application.section_audition(str(run_id), section_id),
            "Section audio not found.",
        )
        return Response(
            content=payload,
            media_type="audio/wav",
            headers={"Content-Disposition": f'inline; filename="{section_id}.wav"'},
        )

    @router.get("/breath-protocols")
    def breath_protocols() -> dict[str, Any]:
        return {
            "protocols": [
                {
                    **protocol.model_dump(mode="json"),
                    "versioned_id": protocol.versioned_id,
                }
                for protocol in REVIEWED_BREATH_PROTOCOLS.values()
            ]
        }

    @router.get("/meditation-blueprints")
    def meditation_blueprints() -> dict[str, Any]:
        library = load_blueprint_library(
            application.config_directory / "meditation_blueprints.yaml"
        )
        return {
            "blueprints": [
                {
                    **blueprint.model_dump(mode="json"),
                    "versioned_id": blueprint.versioned_id,
                }
                for blueprint in library.blueprints
            ]
        }

    @router.get("/pacing-profiles")
    def pacing_profiles() -> dict[str, Any]:
        path = application.config_directory / "pacing_profiles.yaml"
        registry = (
            PacingProfileRegistry.load(path)
            if path.is_file()
            else PacingProfileRegistry.load_bundled()
        )
        return {
            "profiles": [
                {
                    **profile.model_dump(mode="json"),
                    "versioned_id": profile.versioned_id,
                    "sha256": profile.digest,
                }
                for profile in registry.profiles()
            ]
        }

    @router.get("/runs/{run_id}/artifact/{artifact}", response_class=FileResponse)
    def artifact(run_id: UUID, artifact: str) -> FileResponse:
        path = _not_found(
            application.artifact_path(str(run_id), artifact),
            "Artifact not found.",
        )
        return FileResponse(path)

    @router.post("/generate", status_code=202)
    def generate(body: GenerationRequest) -> dict[str, Any]:
        return application.start_generation(body.model_dump(exclude_none=True))

    @router.post("/runs/{run_id}/cancel", status_code=202)
    def cancel(run_id: UUID, _body: EmptyBody) -> dict[str, Any]:
        return _not_found(application.cancel_task(str(run_id)), "Run not found.")

    @router.post("/runs/{run_id}/resume", status_code=202)
    def resume(run_id: UUID, _body: EmptyBody) -> dict[str, Any]:
        return _not_found(application.resume_run(str(run_id)), "Run not found.")

    @router.post("/runs/{run_id}/segments/{segment_id}/regenerate", status_code=202)
    def regenerate(run_id: UUID, segment_id: str, _body: EmptyBody) -> dict[str, Any]:
        if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
            raise ValueError("Invalid segment ID.")
        return _not_found(
            application.regenerate_segment(str(run_id), segment_id),
            "Run not found.",
        )

    @router.get("/runs/{run_id}/segments/{segment_id}/takes")
    def segment_takes(run_id: UUID, segment_id: str) -> dict[str, Any]:
        if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
            raise ValueError("Invalid segment ID.")
        return _not_found(
            application.list_segment_takes(str(run_id), segment_id),
            "Run not found.",
        )

    @router.post("/runs/{run_id}/segments/{segment_id}/takes", status_code=202)
    def generate_segment_take(
        run_id: UUID,
        segment_id: str,
        _body: EmptyBody,
    ) -> dict[str, Any]:
        if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
            raise ValueError("Invalid segment ID.")
        return _not_found(
            application.generate_alternative_take(str(run_id), segment_id),
            "Run not found.",
        )

    @router.post("/runs/{run_id}/segments/{segment_id}/takes/undo", status_code=202)
    def undo_segment_take(
        run_id: UUID,
        segment_id: str,
        _body: EmptyBody,
    ) -> dict[str, Any]:
        if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
            raise ValueError("Invalid segment ID.")
        return _not_found(
            application.choose_segment_take(str(run_id), segment_id, "", undo=True),
            "Run not found.",
        )

    @router.post(
        "/runs/{run_id}/segments/{segment_id}/takes/{take_id}/choose",
        status_code=202,
    )
    def choose_segment_take(
        run_id: UUID,
        segment_id: str,
        take_id: str,
        _body: EmptyBody,
    ) -> dict[str, Any]:
        if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
            raise ValueError("Invalid segment ID.")
        return _not_found(
            application.choose_segment_take(str(run_id), segment_id, take_id),
            "Run not found.",
        )

    @router.get(
        "/runs/{run_id}/segments/{segment_id}/takes/{take_id}/audio",
        response_class=FileResponse,
    )
    def segment_take_audio(run_id: UUID, segment_id: str, take_id: str) -> FileResponse:
        if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
            raise ValueError("Invalid segment ID.")
        path = _not_found(
            application.segment_take_audio_path(str(run_id), segment_id, take_id),
            "Take audio not found.",
        )
        return FileResponse(path, media_type="audio/wav", filename=f"{take_id}.wav")

    @router.get("/runs/{run_id}/revisions")
    def revisions(run_id: UUID) -> dict[str, Any]:
        values = _not_found(application.list_revisions(str(run_id)), "Run not found.")
        return {"revisions": values}

    @router.post("/runs/{run_id}/revisions", status_code=202)
    def create_revision(run_id: UUID, body: RevisionRequest) -> dict[str, Any]:
        return _not_found(
            application.edit_run(str(run_id), body.model_dump(mode="json")),
            "Run not found.",
        )

    @router.post("/runs/{run_id}/revisions/{revision_id}/restore", status_code=202)
    def restore_revision(run_id: UUID, revision_id: str, _body: EmptyBody) -> dict[str, Any]:
        return _not_found(
            application.restore_revision(str(run_id), revision_id),
            "Run not found.",
        )

    @router.get("/runs/{run_id}/exports")
    def exports(run_id: UUID) -> dict[str, Any]:
        return _not_found(application.exports(str(run_id)), "Run not found.")

    @router.post("/runs/{run_id}/exports", status_code=201)
    def create_exports(run_id: UUID, _body: EmptyBody) -> dict[str, Any]:
        return _not_found(application.export_run(str(run_id)), "Run not found.")

    @router.get("/runs/{run_id}/exports/{kind}", response_class=FileResponse)
    def download_export(run_id: UUID, kind: str) -> FileResponse:
        if kind not in {"wav", "flac", "aac", "opus"}:
            raise HTTPException(status_code=404, detail="Export not found.")
        path = _not_found(application.export_path(str(run_id), kind), "Export not found.")
        return FileResponse(path, filename=path.name)

    @router.delete("/runs/{run_id}")
    def delete_run(run_id: UUID) -> dict[str, Any]:
        result = _not_found(application.delete_run(str(run_id)), "Run not found.")
        catalog.replace_runs(application.list_runs())
        return result

    @router.get("/lab/evaluation-sets")
    def lab_evaluation_sets() -> dict[str, Any]:
        return {"evaluation_sets": lab.evaluation_sets()}

    @router.get("/lab/evaluation-sets/{evaluation_id}/versions/{version}/acceptance")
    def lab_evaluation_acceptance(evaluation_id: str, version: int) -> dict[str, Any]:
        evaluation = lab.evaluation_set_by_id(evaluation_id, version)
        return assess_corpus_acceptance(
            evaluation,
            load_lab_acceptance_evidence(lab.repository.root),
        ).model_dump(mode="json")

    @router.get("/lab/decisions")
    def lab_decisions() -> dict[str, Any]:
        return {
            "decisions": [
                decision.model_dump(mode="json") for decision in lab.repository.decisions()
            ]
        }

    @router.get("/lab/capabilities")
    def lab_capabilities() -> dict[str, Any]:
        return lab.capabilities()

    @router.get("/lab/experiments")
    def lab_experiments() -> dict[str, Any]:
        refresh_lab_catalog()
        return lab.experiments()

    @router.post("/lab/experiments", status_code=201)
    def create_lab_experiment(definition: ExperimentDefinition) -> dict[str, Any]:
        result = lab.add_definition(definition)
        refresh_lab_catalog()
        return result

    @router.post(
        "/lab/experiments/{experiment_id}/versions/{version}/execute",
        status_code=202,
    )
    def execute_lab_experiment(
        experiment_id: str,
        version: int,
        body: ExperimentRunRequest,
    ) -> dict[str, Any]:
        try:
            return lab.execute(
                experiment_id=experiment_id,
                version=version,
                case_ids=body.case_ids,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.post(
        "/lab/experiments/{experiment_id}/versions/{version}/pause",
        status_code=202,
    )
    def pause_lab_experiment(experiment_id: str, version: int, _body: EmptyBody) -> dict[str, Any]:
        try:
            return lab.halt(experiment_id=experiment_id, version=version, action="pause")
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.post(
        "/lab/experiments/{experiment_id}/versions/{version}/cancel",
        status_code=202,
    )
    def cancel_lab_experiment(experiment_id: str, version: int, _body: EmptyBody) -> dict[str, Any]:
        try:
            return lab.halt(experiment_id=experiment_id, version=version, action="cancel")
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.post(
        "/lab/experiments/{experiment_id}/versions/{version}/resume",
        status_code=202,
    )
    def resume_lab_experiment(experiment_id: str, version: int, _body: EmptyBody) -> dict[str, Any]:
        try:
            return lab.execute(experiment_id=experiment_id, version=version, case_ids=None)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @router.get("/lab/experiments/{experiment_id}/versions/{version}/report")
    def lab_report(
        experiment_id: str,
        version: int,
        reveal: bool = Query(default=False),
    ) -> dict[str, Any]:
        try:
            report = lab.report(experiment_id, version)
        except EvaluationStorageError as error:
            raise HTTPException(status_code=404, detail="Experiment report not found.") from error
        refresh_lab_catalog()
        payload = report.model_dump(mode="json")
        payload["identities_revealed"] = reveal
        if reveal:
            evaluation = lab._evaluation_set(report.definition)
            review = blind_review_status(
                report,
                expected_case_ids=(item.id for item in evaluation.cases),
            )
            if not review["promotion_ready"]:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Candidate identities remain sealed until the complete versioned "
                        "evaluation set has every required blind rating and preference."
                    ),
                )
            return payload
        definition = payload["definition"]
        definition.pop("candidates", None)
        definition.pop("baseline_candidate_id", None)
        # Trial UUIDs are deterministic from candidate IDs, and source run IDs
        # link directly to unsealed manifests. Exposing either would defeat the
        # reviewer-safe projection even if candidate_id itself were removed.
        payload["trials"] = [
            {"case_id": trial.case_id, "success": trial.success} for trial in report.trials
        ]
        payload["incomplete_candidates_by_case"] = {
            case_id: len(candidate_ids)
            for case_id, candidate_ids in report.incomplete_candidates_by_case.items()
        }
        payload["regressions"] = []
        payload["blind_assignments"] = [
            value.model_dump(mode="json") for value in blind_presentations(report)
        ]
        return payload

    @router.get("/lab/experiments/{experiment_id}/versions/{version}/blind")
    def lab_blind_presentations(experiment_id: str, version: int) -> dict[str, Any]:
        try:
            report = lab.report(experiment_id, version)
        except EvaluationStorageError as error:
            raise HTTPException(status_code=404, detail="Experiment report not found.") from error
        evaluation = lab._evaluation_set(report.definition)
        return {
            "presentations": [
                value.model_dump(mode="json") for value in blind_presentations(report)
            ],
            "review": blind_review_status(
                report,
                expected_case_ids=(item.id for item in evaluation.cases),
            ),
        }

    @router.get(
        "/lab/experiments/{experiment_id}/versions/{version}/blind-media/{media_token}",
        response_class=FileResponse,
    )
    def lab_blind_audio(
        experiment_id: str,
        version: int,
        media_token: UUID,
    ) -> FileResponse:
        try:
            artifact = resolve_blind_audio(lab.report(experiment_id, version), media_token)
        except (EvaluationExecutionError, EvaluationStorageError) as error:
            raise HTTPException(status_code=404, detail="Experiment report not found.") from error
        path = Path(artifact.path)
        if not path.is_absolute():
            path = application.project_root / path
        path = path.resolve()
        try:
            path.relative_to(application.runs_directory.resolve())
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Blind audio not found.") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Blind audio not found.")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as error:
            raise HTTPException(status_code=404, detail="Blind audio not found.") from error
        if digest.hexdigest() != artifact.sha256 or not artifact.media_type.startswith("audio/"):
            raise HTTPException(
                status_code=409,
                detail="Blind audio no longer matches its immutable experiment evidence.",
            )
        return FileResponse(path, media_type=artifact.media_type, filename="blind-meditation.wav")

    @router.post(
        "/lab/experiments/{experiment_id}/versions/{version}/ratings",
        status_code=201,
    )
    def rate_lab_experiment(
        experiment_id: str,
        version: int,
        body: HumanRatingRequest,
    ) -> dict[str, Any]:
        try:
            report = lab.report(experiment_id, version)
        except EvaluationStorageError as error:
            raise HTTPException(status_code=404, detail="Experiment report not found.") from error
        review = blind_review_status(report)
        if body.dimension not in review["required_dimensions"]:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.dimension} is not a required rating dimension for this "
                    f"{report.definition.experiment_type} experiment."
                ),
            )
        if body.dimension == review["preference_dimension"] and body.preferred_side is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A {body.dimension} rating must explicitly prefer left, right, or tie; "
                    "otherwise this immutable rating could not satisfy blind review."
                ),
            )
        if body.dimension != review["preference_dimension"] and body.preferred_side is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Only the {review['preference_dimension']} rating records the blind "
                    "left/right/tie preference."
                ),
            )
        rating = HumanRating(
            rating_id=uuid4(),
            assignment_id=body.assignment_id,
            dimension=body.dimension,
            score=body.score,
            preferred_side=body.preferred_side,
            notes=body.notes,
            created_at=datetime.now(UTC),
        )
        try:
            add_rating(report, rating)
            lab.repository.save_rating(
                experiment_id=experiment_id,
                experiment_version=version,
                rating=rating,
            )
        except (EvaluationExecutionError, EvaluationStorageError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        refresh_lab_catalog()
        return rating.model_dump(mode="json")

    @router.get("/lab/experiments/{experiment_id}/versions/{version}/regressions")
    def lab_regressions(
        experiment_id: str,
        version: int,
        candidate_id: str = Query(min_length=1, max_length=64),
        baseline_experiment_id: str = Query(min_length=1, max_length=96),
        baseline_version: int = Query(ge=1),
        baseline_candidate_id: str = Query(min_length=1, max_length=64),
    ) -> dict[str, Any]:
        try:
            report = lab.report(experiment_id, version)
            baseline = lab.report(baseline_experiment_id, baseline_version)
        except EvaluationStorageError as error:
            raise HTTPException(status_code=404, detail="Experiment report not found.") from error
        return {
            "regressions": [
                value.model_dump(mode="json")
                for value in compare_against_baseline(
                    report,
                    baseline,
                    candidate_id=candidate_id,
                    baseline_candidate_id=baseline_candidate_id,
                )
            ]
        }

    @router.post(
        "/lab/experiments/{experiment_id}/versions/{version}/decisions",
        status_code=201,
    )
    def decide_lab_experiment(
        experiment_id: str,
        version: int,
        body: PromotionRequest,
    ) -> dict[str, Any]:
        try:
            report = lab.report(experiment_id, version)
        except EvaluationStorageError as error:
            raise HTTPException(status_code=404, detail="Experiment report not found.") from error
        selected_candidate = next(
            (
                candidate
                for candidate in report.definition.candidates
                if candidate.candidate_id == body.candidate_id
            ),
            None,
        )
        if (
            body.outcome == "promote"
            and report.definition.experiment_type == "voice"
            and selected_candidate is not None
        ):
            promotion_error = _studio_voice_promotion_error(
                application.environment_status(), selected_candidate.condition.voice.model_id
            )
            if promotion_error is not None:
                raise HTTPException(status_code=409, detail=promotion_error)
        if selected_candidate is None:
            raise HTTPException(status_code=422, detail="Decision candidate is not in the report.")
        if body.outcome == "promote":
            condition = selected_candidate.condition
            if condition.voice.voice_mode == "reference":
                try:
                    application.voice_reference_manager.load(condition.voice.voice_id)
                except (OSError, ValueError) as error:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "The selected candidate's consented voice reference is no longer "
                            "available or valid. Restore it before promotion."
                        ),
                    ) from error
            if report.definition.experiment_type == "pacing":
                profile_path = application.config_directory / "pacing_profiles.yaml"
                registry = (
                    PacingProfileRegistry.load(profile_path)
                    if profile_path.is_file()
                    else PacingProfileRegistry.load_bundled()
                )
                try:
                    profile = registry.get(condition.pacing_profile_id)
                except ValueError as error:
                    raise HTTPException(
                        status_code=409,
                        detail="The selected pacing profile is no longer installed.",
                    ) from error
                candidate_hashes = {
                    trial.controlled_inputs.pacing_profile_sha256
                    for trial in report.trials
                    if trial.candidate_id == selected_candidate.candidate_id
                    and trial.success
                    and trial.controlled_inputs is not None
                }
                if candidate_hashes != {profile.digest}:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "The reviewed pacing profile changed after this experiment. "
                            "Run a new comparison revision before promotion."
                        ),
                    )
        baseline_candidate = next(
            (
                candidate
                for candidate in report.definition.candidates
                if candidate.candidate_id == report.definition.baseline_candidate_id
            ),
            report.definition.candidates[0],
        )
        previous_value = _candidate_product_value(
            report.definition.experiment_type,
            baseline_candidate,
        )
        selected_value = _candidate_product_value(
            report.definition.experiment_type,
            selected_candidate,
        )
        if (
            body.outcome == "promote"
            and report.definition.experiment_type == "llm"
            and body.selection_role in {"portable_default", "low_resource_fallback"}
        ):
            other_role = (
                "low_resource_fallback"
                if body.selection_role == "portable_default"
                else "portable_default"
            )
            if any(
                decision.outcome == "promote"
                and decision.selection_role == other_role
                and decision.selected_value == selected_value
                for decision in lab.repository.decisions()
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Portable default and low-resource fallback must be different "
                        "evidence-backed models."
                    ),
                )
        report_sha256 = lab.repository.report_sha256(
            experiment_id=experiment_id,
            version=version,
        )
        decision = create_promotion_decision(
            report,
            outcome=body.outcome,
            candidate_id=body.candidate_id,
            previous_value=previous_value,
            selected_value=selected_value,
            rationale=body.rationale,
            report_sha256=report_sha256,
            selection_role=body.selection_role,
            expected_case_ids=(item.id for item in lab._evaluation_set(report.definition).cases),
            supported_low_resource_llm_ids=_supported_low_resource_llm_ids(),
        )
        lab.repository.save_decision(decision)
        refresh_lab_catalog()
        if decision.outcome == "promote":
            for setting_key in _promotion_setting_keys(decision):
                catalog.set_setting(
                    setting_key,
                    {
                        "schema_version": 2,
                        "value": decision.selected_value,
                        "candidate_id": decision.candidate_id,
                        "selection_role": decision.selection_role,
                        "decision_id": str(decision.decision_id),
                        "experiment_id": decision.experiment_id,
                        "experiment_version": decision.experiment_version,
                        "report_sha256": decision.report_sha256,
                        "selected_condition": (
                            selected_candidate.condition.model_dump(mode="json")
                            if selected_candidate is not None
                            else None
                        ),
                        "promoted_at": decision.created_at.isoformat(),
                    },
                )
            application._invalidate_environment_status()
        return decision.model_dump(mode="json")

    @router.get("/settings/export")
    def export_settings() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "settings": catalog.export_settings(),
            "local_data": {
                "runs": str(application.runs_directory),
                "models": str(application.models_directory),
                "catalog": str(catalog.path),
                "lab_evidence": str(lab.repository.root),
            },
        }

    @router.post("/settings/import")
    def import_settings(body: SettingsImport) -> dict[str, Any]:
        imported = catalog.import_settings(body.settings)
        application._invalidate_environment_status()
        return {
            "schema_version": 1,
            "settings": imported,
        }

    app.include_router(router, prefix=f"/api/v{API_VERSION}")
    app.include_router(router, prefix="/api", include_in_schema=False)

    static_root = Path(__file__).with_name("static")
    app.mount("/", ProductStaticFiles(directory=static_root, html=True), name="application")
    return app
