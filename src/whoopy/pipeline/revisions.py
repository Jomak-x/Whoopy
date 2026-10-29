"""Immutable meditation revisions and selective re-render preparation."""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from whoopy.meditation import (
    REVIEWED_BREATH_PROTOCOLS,
    MeditationGenerationResult,
    MeditationPlan,
    load_blueprint_library,
)
from whoopy.meditation.generator import TECHNIQUE_DELIVERY
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.runs import RunExecution, RunStage, RunStatus, RunStore, RunStoreError
from whoopy.timeline import (
    BreathSegment,
    DeliveryIntensity,
    DeliveryStyle,
    PaceIntent,
    PronunciationOverride,
    SilenceSegment,
    SpeechSegment,
    Timeline,
)
from whoopy.timeline.compiler import ScriptControls, compile_script, rewrite_script_pauses

REVISION_ROOT = "revisions"
ACTIVE_REVISION = "active.json"
REVISION_DRAFT_SECTIONS = "draft-sections"
REVISION_RAW_ATTEMPTS = "raw-model-output"


class RevisionError(RunStoreError):
    """Raised when an edit cannot safely replace the active revision."""


def _ordered_control_alignment(
    previous: list[str], requested: list[str], *, prefer_late: bool
) -> set[tuple[int, int]]:
    matches: set[tuple[int, int]] = set()

    def lengths(old_indices: range, new_indices: range) -> list[int]:
        row = [0] * (len(new_indices) + 1)
        for old in old_indices:
            diagonal = 0
            for column, new in enumerate(new_indices, start=1):
                above = row[column]
                row[column] = (
                    diagonal + 1 if previous[old] == requested[new] else max(above, row[column - 1])
                )
                diagonal = above
        return row

    def align(old_start: int, old_end: int, new_start: int, new_end: int) -> None:
        if old_start == old_end or new_start == new_end:
            return
        if old_end - old_start == 1:
            columns = range(new_start, new_end)
            for column in reversed(columns) if prefer_late else columns:
                if previous[old_start] == requested[column]:
                    matches.add((old_start, column))
                    return
            return
        middle = (old_start + old_end) // 2
        prefix = lengths(range(old_start, middle), range(new_start, new_end))
        suffix = lengths(range(old_end - 1, middle - 1, -1), range(new_end - 1, new_start - 1, -1))
        width = new_end - new_start
        crossing = max(
            range(width + 1),
            key=lambda column: (
                prefix[column] + suffix[width - column],
                column if prefer_late else -column,
            ),
        )
        del prefix, suffix
        align(old_start, middle, new_start, new_start + crossing)
        align(middle, old_end, new_start + crossing, new_end)

    align(0, len(previous), 0, len(requested))
    return matches


def _control_correspondence(
    previous: ScriptControls, requested: ScriptControls
) -> dict[str, dict[tuple[int, int], tuple[int, int]]]:
    correspondence: dict[str, dict[tuple[int, int], tuple[int, int]]] = {}
    for name, requested_markers in requested.markers.items():
        previous_markers = previous.markers.get(name, {})
        if list(previous_markers.values()) == list(requested_markers.values()):
            correspondence[name] = dict(zip(requested_markers, previous_markers, strict=True))
            continue
        previous_values = list(previous_markers.values())
        requested_values = list(requested_markers.values())
        mandatory = _ordered_control_alignment(
            previous_values, requested_values, prefer_late=False
        ) & _ordered_control_alignment(previous_values, requested_values, prefer_late=True)
        previous_origins = list(previous_markers)
        requested_origins = list(requested_markers)
        correspondence[name] = {
            requested_origins[new]: previous_origins[old] for old, new in mandatory
        }
    return correspondence


def _same_control_origin(
    previous: tuple[int, int] | None,
    requested: tuple[int, int] | None,
    correspondence: dict[tuple[int, int], tuple[int, int]],
) -> bool:
    if previous is None or requested is None:
        return previous is requested
    return correspondence.get(requested) == previous


class TimelineEdit(BaseModel):
    """Validated changes that preserve canonical typed timeline controls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    silence_duration_ms: dict[str, int] = Field(default_factory=dict)
    delivery: dict[str, DeliveryStyle] = Field(default_factory=dict)
    intensity: dict[str, DeliveryIntensity] = Field(default_factory=dict)
    pace: dict[str, PaceIntent] = Field(default_factory=dict)
    pronunciation_overrides: dict[str, tuple[PronunciationOverride, ...]] = Field(
        default_factory=dict
    )


class BreathProtocolEdit(BaseModel):
    """A reviewed no-hold protocol switch for an existing compiled breath block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_id: str = Field(pattern=r"^[a-z][a-z0-9_-]+@[1-9][0-9]*$")
    cycles: int = Field(ge=2, le=12)


class RevisionRequest(BaseModel):
    """User-editable artifacts; omitted values remain byte-for-byte unchanged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    script: str | None = Field(default=None, min_length=1, max_length=200_000)
    plan: MeditationPlan | None = None
    timeline: TimelineEdit = Field(default_factory=TimelineEdit)
    breath_protocol: BreathProtocolEdit | None = None
    note: str = Field(default="Edited in Whoopy Studio.", min_length=1, max_length=500)


class RevisionManifest(BaseModel):
    """Immutable provenance for one accepted local edit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    revision_id: str
    run_id: UUID
    parent_revision_id: str | None
    created_at: datetime
    note: str
    change_kind: Literal["initial", "assembly_only", "selective_speech", "script", "plan"]
    affected_speech_segment_ids: tuple[str, ...] = ()
    artifacts: tuple[str, ...]


def validate_semantic_plan_edit(
    current: MeditationPlan,
    requested: MeditationPlan,
    *,
    blueprint_path: Path = Path("config/meditation_blueprints.yaml"),
) -> MeditationPlan:
    """Accept only reviewed semantic changes while keeping deterministic budgets fixed."""

    blueprint = load_blueprint_library(blueprint_path).for_kind(requested.meditation_kind)
    if requested.blueprint_id != blueprint.versioned_id:
        raise RevisionError(
            f"{requested.meditation_kind} requires reviewed blueprint {blueprint.versioned_id}."
        )
    if requested.blueprint_version != blueprint.version:
        raise RevisionError("The edited plan uses an unsupported blueprint version.")
    if requested.breath_mode != blueprint.breath_mode:
        raise RevisionError("Breathing mode is fixed by the reviewed technique blueprint.")
    if requested.ending_behavior != blueprint.ending_behavior:
        raise RevisionError("Ending behavior is fixed by the reviewed technique blueprint.")
    fixed_plan_fields = (
        "schema_version",
        "requested_duration_seconds",
        "words_per_minute",
        "silence_budget_seconds",
    )
    for name in fixed_plan_fields:
        if getattr(requested, name) != getattr(current, name):
            raise RevisionError(f"Plan editing cannot change deterministic field {name}.")
    if [section.id for section in requested.sections] != [
        section.id for section in current.sections
    ]:
        raise RevisionError("Plan editing preserves section IDs, count, and order.")
    beats_by_count: dict[int, tuple[str, ...]] = {
        3: ("arrival", "practice", "ending"),
        4: ("arrival", "instruction", "deeper_practice", "ending"),
        5: ("arrival", "instruction", "practice", "reminder", "ending"),
        6: (
            "arrival",
            "instruction",
            "practice",
            "reminder",
            "deeper_practice",
            "ending",
        ),
    }
    if (
        tuple(section.beat for section in requested.sections)
        != beats_by_count[len(requested.sections)]
    ):
        raise RevisionError("Technique beats must follow the reviewed progression.")
    expected_ending = "sleep_transition" if requested.meditation_kind == "sleep" else "return"
    if requested.sections[0].technique != "arrival":
        raise RevisionError("The arrival beat must use the reviewed arrival technique.")
    if requested.sections[-1].technique != expected_ending:
        raise RevisionError(f"The ending beat must use {expected_ending}.")
    normalized_sections = []
    for original, edited in zip(current.sections, requested.sections, strict=True):
        for name in (
            "pause_intent",
            "target_speech_seconds",
            "pause_after_ms",
            "minimum_words",
            "maximum_words",
        ):
            if getattr(edited, name) != getattr(original, name):
                raise RevisionError(f"Plan editing cannot change deterministic field {name}.")
        if edited.technique not in blueprint.allowed_techniques:
            raise RevisionError(
                f"Technique {edited.technique!r} is not reviewed for {requested.meditation_kind}."
            )
        normalized_sections.append(
            edited.model_copy(update={"delivery": TECHNIQUE_DELIVERY[edited.technique]})
        )
    normalized = requested.model_copy(update={"sections": normalized_sections})
    if normalized == current:
        raise RevisionError("The semantic plan edit does not change the active plan.")
    return normalized


class RevisionStore:
    """Snapshot, validate, activate, and restore immutable run revisions."""

    def __init__(self, store: RunStore, *, run_lock: RunLock | None = None) -> None:
        self.store = store
        self.run_lock = run_lock

    def active_revision_id(self, run_id: UUID | str) -> str | None:
        active = self._active(self.store.parse_run_id(run_id))
        return active.revision_id if active is not None else None

    def list(self, run_id: UUID | str) -> list[RevisionManifest]:
        root = self._root(run_id)
        manifests: list[RevisionManifest] = []
        if not root.is_dir():
            return manifests
        for path in sorted(root.glob("r*/revision.json")):
            try:
                manifests.append(RevisionManifest.model_validate_json(path.read_text()))
            except (OSError, ValueError):
                continue
        return manifests

    def create(self, run_id: UUID | str, request: RevisionRequest) -> RevisionManifest:
        parsed = self.store.parse_run_id(run_id)
        try:
            with RunLock(self.store.run_directory(parsed)):
                record = self.store.load(parsed)
                if record.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                    raise RevisionError("Wait for generation to stop before editing.")
                old_timeline = self.store.load_timeline(parsed)
                script = self.store.load_script(parsed)
                current_plan = self._load_plan(parsed)
                if request.plan is not None and request.plan != current_plan:
                    raise RevisionError(
                        "Semantic plan edits require a production plan regenerator, but "
                        "this revision workflow has no drafting context. Start a new "
                        "prompt generation for the changed technique, breathing mode, "
                        "beats, or outcome; the existing run was not changed."
                    )
                new_timeline = old_timeline
                active_plan = current_plan
                change_kind: Literal[
                    "initial", "assembly_only", "selective_speech", "script", "plan"
                ] = "assembly_only"
                affected: set[str] = set()
                changed = False
                if request.script is not None and request.script != script:
                    previous_script_timeline, _, previous_controls = compile_script(
                        run_id=parsed,
                        script=script,
                        created_at=old_timeline.created_at,
                        source=cast(
                            Literal["script_file", "generated_prompt"],
                            old_timeline.source,
                        ),
                    )
                    script = request.script
                    new_timeline, _, requested_controls = compile_script(
                        run_id=parsed,
                        script=script,
                        created_at=datetime.now(UTC),
                        source=cast(
                            Literal["script_file", "generated_prompt"],
                            old_timeline.source,
                        ),
                    )
                    previous_pauses = [
                        segment.duration_ms
                        for segment in previous_script_timeline.segments
                        if isinstance(segment, SilenceSegment)
                    ]
                    requested_pauses = [
                        segment.duration_ms
                        for segment in new_timeline.segments
                        if isinstance(segment, SilenceSegment)
                    ]
                    previous_segments = previous_script_timeline.segments
                    requested_segments = new_timeline.segments
                    current_segments = {segment.id: segment for segment in old_timeline.segments}
                    previous_keys = [
                        segment.model_dump_json(
                            exclude={"id", "delivery", "intensity", "pace", "voice_mode"}
                        )
                        for segment in previous_segments
                    ]
                    requested_keys = [
                        segment.model_dump_json(
                            exclude={"id", "delivery", "intensity", "pace", "voice_mode"}
                        )
                        for segment in requested_segments
                    ]
                    mandatory = _ordered_control_alignment(
                        previous_keys, requested_keys, prefer_late=False
                    ) & _ordered_control_alignment(previous_keys, requested_keys, prefer_late=True)
                    correspondence = {new: old for old, new in mandatory}
                    ambiguous: dict[int, list[int]] = {}
                    split_children: dict[int, list[SpeechSegment]] = {}
                    boundaries = [(-1, -1), *sorted(mandatory)]
                    boundaries.append((len(previous_keys), len(requested_keys)))
                    for (old_start, new_start), (old_end, new_end) in zip(
                        boundaries, boundaries[1:], strict=False
                    ):
                        old_indices = range(old_start + 1, old_end)
                        new_indices = range(new_start + 1, new_end)
                        matches = {
                            new: [
                                old
                                for old in old_indices
                                if previous_keys[old] == requested_keys[new]
                            ]
                            for new in new_indices
                        }
                        if any(matches.values()):
                            matched_old = {
                                old for candidates in matches.values() for old in candidates
                            }
                            unmatched_old = [old for old in old_indices if old not in matched_old]
                            ambiguous.update(
                                {
                                    new: candidates or unmatched_old
                                    for new, candidates in matches.items()
                                }
                            )
                        elif len(old_indices) == len(new_indices):
                            correspondence.update(zip(new_indices, old_indices, strict=True))
                        elif (
                            len(old_indices) == 1
                            and len(new_indices) > 1
                            and isinstance(previous_segments[old_indices.start], SpeechSegment)
                            and all(
                                isinstance(requested_segments[new], SpeechSegment)
                                for new in new_indices
                            )
                            and cast(SpeechSegment, previous_segments[old_indices.start]).text
                            == " ".join(
                                cast(SpeechSegment, requested_segments[new]).text
                                for new in new_indices
                            )
                        ):
                            correspondence.update(dict.fromkeys(new_indices, old_indices.start))
                            split_children[old_indices.start] = [
                                cast(SpeechSegment, requested_segments[new]) for new in new_indices
                            ]
                        else:
                            ambiguous.update({new: list(old_indices) for new in new_indices})
                    control_correspondence = _control_correspondence(
                        previous_controls, requested_controls
                    )
                    merged_segments = []
                    for index, requested in enumerate(requested_segments):
                        previous_index = correspondence.get(index)
                        candidates = (
                            [previous_index]
                            if previous_index is not None
                            else ambiguous.get(index, [])
                        )
                        updates: dict[str, object] = {}
                        for candidate in candidates:
                            previous = previous_segments[candidate]
                            current = current_segments.get(previous.id)
                            if (
                                isinstance(previous, SpeechSegment)
                                and isinstance(current, SpeechSegment)
                                and isinstance(requested, SpeechSegment)
                                and previous.text == current.text
                            ):
                                if (
                                    (candidate in split_children or previous_index is None)
                                    and previous.text != requested.text
                                    and current.target_duration_ms is not None
                                ):
                                    raise RevisionError(
                                        "This script edit changes the boundaries of speech with "
                                        "an accepted target duration; keep that speech together."
                                    )
                                children = split_children.get(candidate, [])
                                if (
                                    children
                                    and any(
                                        not any(override.source in child.text for child in children)
                                        for override in current.pronunciation_overrides
                                    )
                                    and any(
                                        child.id not in request.timeline.pronunciation_overrides
                                        for child in children
                                    )
                                ):
                                    raise RevisionError(
                                        "This script edit splits an accepted pronunciation "
                                        "override; keep its source text together or explicitly "
                                        "set replacement pronunciations."
                                    )
                                for name in ("delivery", "intensity", "pace", "voice_mode"):
                                    if getattr(previous, name) == getattr(requested, name) and (
                                        _same_control_origin(
                                            previous_controls.origins[previous.id].get(name),
                                            requested_controls.origins[requested.id].get(name),
                                            control_correspondence.get(name, {}),
                                        )
                                    ):
                                        updates[name] = getattr(current, name)
                                updates["language"] = current.language
                                updates["pronunciation_overrides"] = tuple(
                                    override
                                    for override in current.pronunciation_overrides
                                    if previous_index is None or override.source in requested.text
                                )
                                if previous.text == requested.text:
                                    updates["target_duration_ms"] = current.target_duration_ms
                            elif (
                                isinstance(previous, SilenceSegment)
                                and isinstance(current, SilenceSegment)
                                and isinstance(requested, SilenceSegment)
                                and previous.duration_ms == requested.duration_ms
                            ):
                                updates["duration_ms"] = current.duration_ms
                            if previous_index is None:
                                for name, value in updates.items():
                                    edit_name = (
                                        "silence_duration_ms" if name == "duration_ms" else name
                                    )
                                    explicit = getattr(request.timeline, edit_name, {})
                                    if (
                                        value != getattr(requested, name)
                                        and requested.id not in explicit
                                    ):
                                        raise RevisionError(
                                            "This script edit ambiguously moves accepted controls; "
                                            "edit repeated paragraphs separately or explicitly set "
                                            "their replacement controls."
                                        )
                                updates = {}
                        merged_segments.append(requested.model_copy(update=updates))
                    new_timeline = new_timeline.model_copy(update={"segments": merged_segments})
                    merged_pauses = [
                        segment.duration_ms
                        for segment in new_timeline.segments
                        if isinstance(segment, SilenceSegment)
                    ]
                    generation = (
                        self.store.load_resolved_config(parsed).generation
                        if record.source_kind == "generated_prompt"
                        else None
                    )
                    new_timeline = new_timeline.model_copy(
                        update={
                            "preserve_timing": old_timeline.preserve_timing
                            or (
                                generation is not None
                                and generation.writing_method == "recipe_whole_session"
                            )
                            or requested_pauses != previous_pauses
                            or merged_pauses != requested_pauses
                        }
                    )
                    change_kind = "script"
                    changed = True
                    old_speech = {
                        segment.id: segment
                        for segment in old_timeline.segments
                        if isinstance(segment, SpeechSegment)
                    }
                    affected.update(
                        segment.id
                        for segment in new_timeline.segments
                        if isinstance(segment, SpeechSegment)
                        and old_speech.get(segment.id) != segment
                    )
                new_timeline, timeline_affected, timeline_changed = self._apply_timeline_edits(
                    new_timeline, request.timeline
                )
                affected.update(timeline_affected)
                changed = changed or timeline_changed
                if request.timeline.silence_duration_ms:
                    script = rewrite_script_pauses(
                        script=script,
                        timeline=new_timeline,
                        durations=request.timeline.silence_duration_ms,
                    )
                if request.breath_protocol is not None:
                    new_timeline, script, breath_affected = self._apply_breath_protocol_edit(
                        new_timeline,
                        script,
                        request.breath_protocol,
                    )
                    affected.update(breath_affected)
                    changed = True
                    if active_plan is not None and active_plan.breath_mode != "paced_no_hold":
                        active_plan = active_plan.model_copy(
                            update={"breath_mode": "paced_no_hold"}
                        )
                if affected and change_kind != "script":
                    change_kind = "selective_speech"
                if not changed:
                    raise RevisionError(
                        "The revision does not change the active script, timeline, or plan."
                    )
                current_revision = self._ensure_initial(parsed)
                manifest = self._write_revision(
                    parsed,
                    parent=current_revision.revision_id,
                    script=script,
                    timeline=new_timeline,
                    plan=active_plan,
                    note=request.note,
                    change_kind=change_kind,
                    affected=affected,
                )
                self._activate(parsed, manifest, script=script, timeline=new_timeline)
                self._mark_for_render(record, manifest)
                return manifest
        except RunLockUnavailable as error:
            raise RevisionError(f"Run {parsed} is owned by an active worker.") from error

    def create_semantic_plan_revision(
        self,
        run_id: UUID | str,
        *,
        base_plan: MeditationPlan,
        base_revision_id: str | None,
        requested_plan: MeditationPlan,
        generated: MeditationGenerationResult,
        note: str,
        blueprint_path: Path = Path("config/meditation_blueprints.yaml"),
    ) -> RevisionManifest:
        """Commit production-redrafted plan artifacts if their parent is still active."""

        parsed = self.store.parse_run_id(run_id)
        try:
            with RunLock(self.store.run_directory(parsed)):
                return self.create_semantic_plan_revision_locked(
                    parsed,
                    base_plan=base_plan,
                    base_revision_id=base_revision_id,
                    requested_plan=requested_plan,
                    generated=generated,
                    note=note,
                    blueprint_path=blueprint_path,
                )
        except RunLockUnavailable as error:
            raise RevisionError(f"Run {parsed} is owned by an active worker.") from error

    def create_semantic_plan_revision_locked(
        self,
        run_id: UUID | str,
        *,
        base_plan: MeditationPlan,
        base_revision_id: str | None,
        requested_plan: MeditationPlan,
        generated: MeditationGenerationResult,
        note: str,
        blueprint_path: Path = Path("config/meditation_blueprints.yaml"),
    ) -> RevisionManifest:
        """Commit a redrafted plan while the caller holds the run lock."""

        parsed = self.store.parse_run_id(run_id)
        record = self.store.load(parsed)
        if record.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            raise RevisionError("Wait for generation to stop before editing the plan.")
        if record.source_kind != "generated_prompt":
            raise RevisionError("Only prompt-generated meditations have drafting context.")
        if self.active_revision_id(parsed) != base_revision_id:
            raise RevisionError(
                "The active revision changed while its replacement was drafting; "
                "review the newer revision before trying again."
            )
        current_plan = self._load_plan(parsed)
        if current_plan != base_plan:
            raise RevisionError(
                "The active plan changed while its replacement was drafting; "
                "review the newer revision before trying again."
            )
        normalized = validate_semantic_plan_edit(
            base_plan,
            requested_plan,
            blueprint_path=blueprint_path,
        )
        expected_generated_plan = normalized.model_copy(
            update={
                "silence_budget_seconds": generated.plan.silence_budget_seconds,
            }
        )
        if generated.plan != expected_generated_plan:
            raise RevisionError("The production draft does not match the reviewed edited plan.")
        rendered_silence_seconds = round(
            sum(
                segment.duration_ms
                for segment in generated.timeline.segments
                if isinstance(segment, SilenceSegment)
            )
            / 1_000
        )
        if generated.plan.silence_budget_seconds != rendered_silence_seconds:
            raise RevisionError(
                "The production plan silence budget differs from its canonical timeline."
            )
        if generated.timeline.run_id != parsed:
            raise RevisionError("The production draft belongs to another run.")
        current = self._ensure_initial(parsed)
        old_speech = {
            segment.id: segment
            for segment in self.store.load_timeline(parsed).segments
            if isinstance(segment, SpeechSegment)
        }
        affected = {
            segment.id
            for segment in generated.timeline.segments
            if isinstance(segment, SpeechSegment) and old_speech.get(segment.id) != segment
        }
        # Deleted speech checkpoints cannot be reused either. Including
        # their IDs preserves an honest selective invalidation record.
        affected.update(set(old_speech) - {segment.id for segment in generated.timeline.segments})
        manifest = self._write_revision(
            parsed,
            parent=current.revision_id,
            script=generated.script,
            timeline=generated.timeline,
            plan=generated.plan,
            note=note,
            change_kind="plan",
            affected=affected,
            generated=generated,
        )
        self._activate(
            parsed,
            manifest,
            script=generated.script,
            timeline=generated.timeline,
        )
        self._mark_for_render(record, manifest)
        return manifest

    def restore(self, run_id: UUID | str, revision_id: str) -> RevisionManifest:
        parsed = self.store.parse_run_id(run_id)
        if not revision_id.startswith("r") or "/" in revision_id or ".." in revision_id:
            raise RevisionError("Invalid revision ID.")
        try:
            with RunLock(self.store.run_directory(parsed)):
                record = self.store.load(parsed)
                if record.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                    raise RevisionError("Wait for generation to stop before restoring.")
                directory = self._root(parsed) / revision_id
                try:
                    manifest = RevisionManifest.model_validate_json(
                        (directory / "revision.json").read_text(encoding="utf-8")
                    )
                    script = (directory / "script.md").read_text(encoding="utf-8")
                    timeline = Timeline.model_validate_json(
                        (directory / "timeline.json").read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as error:
                    raise RevisionError(
                        f"Could not restore revision {revision_id}: {error}"
                    ) from error
                self._activate(parsed, manifest, script=script, timeline=timeline)
                restored = manifest.model_copy(
                    update={
                        "change_kind": "script",
                        "affected_speech_segment_ids": tuple(
                            segment.id
                            for segment in timeline.segments
                            if isinstance(segment, SpeechSegment)
                        ),
                    }
                )
                self._mark_for_render(record, restored)
                return manifest
        except RunLockUnavailable as error:
            raise RevisionError(f"Run {parsed} is owned by an active worker.") from error

    def create_assembly_revision(
        self,
        run_id: UUID | str,
        *,
        note: str,
    ) -> RevisionManifest:
        """Record an audio-only take choice and request downstream assembly."""

        parsed = self.store.parse_run_id(run_id)
        try:
            with (
                self.run_lock.reuse(self.store.run_directory(parsed))
                if self.run_lock is not None
                else RunLock(self.store.run_directory(parsed))
            ):
                record = self.store.load(parsed)
                if record.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                    raise RevisionError("Wait for generation to stop before choosing a take.")
                current = self._ensure_initial(parsed)
                script = self.store.load_script(parsed)
                timeline = self.store.load_timeline(parsed)
                manifest = self._write_revision(
                    parsed,
                    parent=current.revision_id,
                    script=script,
                    timeline=timeline,
                    plan=None,
                    note=note,
                    change_kind="assembly_only",
                    affected=set(),
                )
                self._activate(parsed, manifest, script=script, timeline=timeline)
                self._mark_for_render(record, manifest)
                return manifest
        except RunLockUnavailable as error:
            raise RevisionError(f"Run {parsed} is owned by an active worker.") from error

    def _ensure_initial(self, run_id: UUID) -> RevisionManifest:
        existing = self.list(run_id)
        if existing:
            active = self._active(run_id)
            return active or existing[-1]
        return self._write_revision(
            run_id,
            parent=None,
            script=self.store.load_script(run_id),
            timeline=self.store.load_timeline(run_id),
            plan=None,
            note="Original generated artifacts.",
            change_kind="initial",
            affected=set(),
        )

    def _write_revision(
        self,
        run_id: UUID,
        *,
        parent: str | None,
        script: str,
        timeline: Timeline,
        plan: MeditationPlan | None,
        note: str,
        change_kind: Literal["initial", "assembly_only", "selective_speech", "script", "plan"],
        affected: set[str],
        generated: MeditationGenerationResult | None = None,
    ) -> RevisionManifest:
        root = self._root(run_id)
        root.mkdir(parents=True, exist_ok=True)
        revision_id = f"r{len(self.list(run_id)) + 1:04d}-{uuid4().hex[:10]}"
        staging = root / f".{revision_id}.tmp"
        destination = root / revision_id
        artifacts = ["script.md", "timeline.json"]
        try:
            staging.mkdir()
            (staging / "script.md").write_text(script, encoding="utf-8")
            (staging / "timeline.json").write_text(
                timeline.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            active_plan = plan
            if active_plan is None and self.store.plan_path(run_id).is_file():
                active_plan = MeditationPlan.model_validate_json(
                    self.store.plan_path(run_id).read_text(encoding="utf-8")
                )
            if active_plan is not None:
                (staging / "plan.json").write_text(
                    active_plan.model_dump_json(indent=2) + "\n", encoding="utf-8"
                )
                artifacts.append("plan.json")
            if generated is not None:
                section_root = staging / REVISION_DRAFT_SECTIONS
                section_root.mkdir()
                for section in generated.sections:
                    (section_root / f"{section.section_id}.json").write_text(
                        section.model_dump_json(indent=2) + "\n",
                        encoding="utf-8",
                    )
                attempt_root = staging / REVISION_RAW_ATTEMPTS
                attempt_root.mkdir()
                for number, attempt in enumerate(generated.raw_attempts, start=1):
                    safe_stage = attempt.stage.replace(":", "-")
                    (attempt_root / f"{number:03d}-{safe_stage}.json").write_text(
                        attempt.model_dump_json(indent=2) + "\n",
                        encoding="utf-8",
                    )
                artifacts.extend((REVISION_DRAFT_SECTIONS, REVISION_RAW_ATTEMPTS))
            else:
                for name, source in (
                    (REVISION_DRAFT_SECTIONS, self.store.draft_sections_path(run_id)),
                    (
                        REVISION_RAW_ATTEMPTS,
                        self.store.raw_model_output_path(run_id),
                    ),
                ):
                    if source.is_dir():
                        shutil.copytree(source, staging / name)
                        artifacts.append(name)
            manifest = RevisionManifest(
                revision_id=revision_id,
                run_id=run_id,
                parent_revision_id=parent,
                created_at=datetime.now(UTC),
                note=note,
                change_kind=change_kind,
                affected_speech_segment_ids=tuple(sorted(affected)),
                artifacts=tuple(artifacts),
            )
            (staging / "revision.json").write_text(
                manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            staging.replace(destination)
            return manifest
        except (OSError, ValueError) as error:
            raise RevisionError(f"Could not save immutable revision: {error}") from error
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _apply_timeline_edits(
        self, timeline: Timeline, edits: TimelineEdit
    ) -> tuple[Timeline, set[str], bool]:
        known = {segment.id for segment in timeline.segments}
        requested = (
            set(edits.silence_duration_ms)
            | set(edits.delivery)
            | set(edits.intensity)
            | set(edits.pace)
            | set(edits.pronunciation_overrides)
        )
        unknown = sorted(requested - known)
        if unknown:
            raise RevisionError(f"Timeline edit references unknown segments: {unknown}")
        affected: set[str] = set()
        changed = False
        segments = []
        for segment in timeline.segments:
            if segment.id in edits.silence_duration_ms:
                if not isinstance(segment, SilenceSegment):
                    raise RevisionError(f"{segment.id} is not an editable SILENCE segment.")
                duration = edits.silence_duration_ms[segment.id]
                if not 1_000 <= duration <= 600_000:
                    raise RevisionError("Edited silence must be between 1 second and 10 minutes.")
                if duration != segment.duration_ms:
                    segment = segment.model_copy(update={"duration_ms": duration})
                    changed = True
            speech_updates: dict[str, object] = {}
            for name, values in (
                ("delivery", edits.delivery),
                ("intensity", edits.intensity),
                ("pace", edits.pace),
                ("pronunciation_overrides", edits.pronunciation_overrides),
            ):
                if segment.id in values:
                    if not isinstance(segment, SpeechSegment):
                        raise RevisionError(f"{segment.id} is not an editable SPEECH segment.")
                    requested_value = values[segment.id]
                    if name == "pronunciation_overrides":
                        overrides = cast(tuple[PronunciationOverride, ...], requested_value)
                        sources = [override.source for override in overrides]
                        if len(sources) != len(set(sources)):
                            raise RevisionError(
                                f"{segment.id} has duplicate pronunciation override sources."
                            )
                        missing = [source for source in sources if source not in segment.text]
                        if missing:
                            raise RevisionError(
                                f"Pronunciation source {missing[0]!r} does not occur in the "
                                f"clean spoken text for {segment.id}."
                            )
                    if requested_value != getattr(segment, name):
                        speech_updates[name] = requested_value
            if speech_updates:
                segment = segment.model_copy(update=speech_updates)
                affected.add(segment.id)
                changed = True
            segments.append(segment)
        return (
            timeline.model_copy(
                update={
                    "segments": segments,
                    "preserve_timing": timeline.preserve_timing
                    or bool(edits.silence_duration_ms or edits.pace),
                }
            ),
            affected,
            changed,
        )

    def _apply_breath_protocol_edit(
        self,
        timeline: Timeline,
        script: str,
        edit: BreathProtocolEdit,
    ) -> tuple[Timeline, str, set[str]]:
        try:
            protocol = REVIEWED_BREATH_PROTOCOLS[edit.protocol_id]
        except KeyError as error:
            raise RevisionError(
                f"Unknown reviewed breath protocol {edit.protocol_id!r}."
            ) from error
        if not protocol.minimum_cycles <= edit.cycles <= protocol.maximum_cycles:
            raise RevisionError(
                f"{edit.protocol_id} supports {protocol.minimum_cycles}-"
                f"{protocol.maximum_cycles} cycles."
            )
        breath_indexes = [
            index
            for index, segment in enumerate(timeline.segments)
            if isinstance(segment, BreathSegment)
        ]
        if not breath_indexes:
            raise RevisionError(
                "The active timeline has no compiled paced-breathing block to edit."
            )
        old_breaths = [cast(BreathSegment, timeline.segments[index]) for index in breath_indexes]
        existing_cycles = {segment.cycle for segment in old_breaths}
        if existing_cycles != set(range(1, edit.cycles + 1)):
            raise RevisionError(
                "Changing the number of breath cycles requires semantic section regeneration; "
                f"this block currently has {len(existing_cycles)} reviewed cycles."
            )
        if len(old_breaths) != edit.cycles * 2:
            raise RevisionError("The paced-breathing block is incomplete or duplicated.")

        segments = list(timeline.segments)
        affected: set[str] = set()
        updated_script = script
        changed = False
        for index in breath_indexes:
            old = segments[index]
            assert isinstance(old, BreathSegment)
            if index == 0 or not isinstance(segments[index - 1], SpeechSegment):
                raise RevisionError(f"{old.id} has no adjacent reviewed spoken timing instruction.")
            instruction = cast(SpeechSegment, segments[index - 1])
            expected_instruction = (
                f"{old.phase.title()} gently for {old.duration_ms // 1_000} seconds."
            )
            if instruction.text != expected_instruction or old.duration_ms % 1_000:
                raise RevisionError(
                    "The paced-breathing prose was edited outside the reviewed compiler; "
                    "regenerate the section before switching protocols."
                )
            seconds = protocol.inhale_seconds if old.phase == "inhale" else protocol.exhale_seconds
            replacement_instruction = f"{old.phase.title()} gently for {seconds} seconds."
            new = old.model_copy(
                update={
                    "duration_ms": seconds * 1_000,
                    "protocol_id": protocol.versioned_id,
                }
            )
            changed = changed or new != old
            segments[index] = new
            if instruction.text != replacement_instruction:
                segments[index - 1] = instruction.model_copy(
                    update={"text": replacement_instruction}
                )
                affected.add(instruction.id)
                updated_script = self._replace_once(
                    updated_script,
                    instruction.text,
                    replacement_instruction,
                    label=f"spoken instruction before {old.id}",
                )
            marker = re.compile(
                r"\[\s*breath\s*:\s*"
                + re.escape(old.phase)
                + r"\s+"
                + re.escape(str(old.duration_ms // 1_000))
                + r"(?:\.0+)?s\s+cycle="
                + re.escape(str(old.cycle))
                + r"\s+protocol="
                + re.escape(old.protocol_id)
                + r"\s*\]",
                re.IGNORECASE,
            )
            updated_script, count = marker.subn(
                (f"[breath: {new.phase} {seconds}s cycle={new.cycle} protocol={new.protocol_id}]"),
                updated_script,
                count=1,
            )
            if count != 1:
                raise RevisionError(f"Could not locate the script marker for {old.id}.")
        if not changed and not affected:
            raise RevisionError("The reviewed breathing protocol is already active.")
        return timeline.model_copy(update={"segments": segments}), updated_script, affected

    @staticmethod
    def _replace_once(script: str, old: str, new: str, *, label: str) -> str:
        if old not in script:
            raise RevisionError(f"Could not locate {label} in the script.")
        return script.replace(old, new, 1)

    def _load_plan(self, run_id: UUID) -> MeditationPlan | None:
        """Load the active plan without treating a corrupt artifact as editable state."""

        path = self.store.plan_path(run_id)
        if not path.is_file():
            return None
        try:
            return MeditationPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RevisionError(f"The active meditation plan is invalid: {error}") from error

    def _activate(
        self, run_id: UUID, manifest: RevisionManifest, *, script: str, timeline: Timeline
    ) -> None:
        self.store._write_bytes(self.store.script_path(run_id), script.encode("utf-8"))
        self.store.write_timeline(run_id, timeline)
        revision = self._root(run_id) / manifest.revision_id
        plan = revision / "plan.json"
        if plan.is_file():
            self.store._write_bytes(self.store.plan_path(run_id), plan.read_bytes())
        for name, destination in (
            (REVISION_DRAFT_SECTIONS, self.store.draft_sections_path(run_id)),
            (REVISION_RAW_ATTEMPTS, self.store.raw_model_output_path(run_id)),
        ):
            source = revision / name
            if source.is_dir():
                self._replace_directory(source, destination)
        self.store._write_json(
            self._root(run_id) / ACTIVE_REVISION,
            json.dumps({"revision_id": manifest.revision_id}, indent=2) + "\n",
        )

    @staticmethod
    def _replace_directory(source: Path, destination: Path) -> None:
        staging = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        backup = destination.with_name(f".{destination.name}.{uuid4().hex}.backup")
        try:
            shutil.copytree(source, staging)
            if destination.exists():
                destination.replace(backup)
            staging.replace(destination)
            shutil.rmtree(backup, ignore_errors=True)
        except OSError as error:
            if not destination.exists() and backup.exists():
                backup.replace(destination)
            raise RevisionError(f"Could not activate revision directory: {error}") from error
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _mark_for_render(self, record: object, manifest: RevisionManifest) -> None:
        from whoopy.pipeline.runs import RunRecord

        if not isinstance(record, RunRecord) or record.recovery is None:
            raise RevisionError("Run does not support resumable editing.")
        affected = len(manifest.affected_speech_segment_ids)
        speech_total = sum(
            isinstance(segment, SpeechSegment)
            for segment in self.store.load_timeline(record.run_id).segments
        )
        recovery = record.recovery.model_copy(
            update={
                "speech_segments_total": speech_total,
                "speech_segments_completed": min(
                    speech_total,
                    max(0, record.recovery.speech_segments_completed - affected),
                ),
                "failed_segment_id": None,
            }
        )
        stage = RunStage.ASSEMBLING if affected == 0 else RunStage.SYNTHESIZING
        execution = RunExecution(
            stage=stage,
            interruption_kind="editor_revision",
            message=f"Revision {manifest.revision_id} is ready to render.",
        )
        interrupted = record.transition(
            RunStatus.INTERRUPTED,
            updated_at=datetime.now(UTC),
            recovery=recovery,
            execution=execution,
        )
        self.store.save(interrupted)

    def _active(self, run_id: UUID) -> RevisionManifest | None:
        try:
            payload = json.loads((self._root(run_id) / ACTIVE_REVISION).read_text())
            path = self._root(run_id) / payload["revision_id"] / "revision.json"
            return RevisionManifest.model_validate_json(path.read_text())
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _root(self, run_id: UUID | str) -> Path:
        return self.store.run_directory(run_id) / REVISION_ROOT
