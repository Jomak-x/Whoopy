"""Atomic, resumable storage for local text-generation artifacts."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from whoopy.meditation.models import (
    DraftedSection,
    MeditationPlan,
    PlannedSection,
    RawGenerationAttempt,
)
from whoopy.meditation.safety import validate_meditation_text
from whoopy.ports import AdapterMetadata
from whoopy.timeline import Timeline


class GenerationManifest(BaseModel):
    """Identity that prevents accidental resume under different inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    run_id: UUID
    prompt: str = Field(min_length=1, max_length=20_000)
    duration_seconds: int = Field(ge=60, le=1_800)
    seed: int
    plan_prompt_id: str = Field(min_length=1)
    plan_prompt_version: int = Field(ge=1)
    section_prompt_id: str = Field(min_length=1)
    section_prompt_version: int = Field(ge=1)
    adapter: AdapterMetadata


class WorkspaceError(RuntimeError):
    """Raised when saved generation state is missing, corrupt, or incompatible."""


class GenerationWorkspace:
    """Persist validated checkpoints without treating raw output as trusted."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        except OSError as error:
            raise WorkspaceError(f"Could not write {path}: {error}") from error
        finally:
            temporary.unlink(missing_ok=True)

    def prepare(self, manifest: GenerationManifest) -> None:
        """Create a workspace or prove that its existing request is identical."""

        path = self.root / "request.json"
        if path.exists():
            try:
                saved = GenerationManifest.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError) as error:
                raise WorkspaceError(f"Invalid generation request record: {error}") from error
            if saved != manifest:
                raise WorkspaceError(
                    "This draft directory belongs to different inputs or model settings."
                )
            return
        self._write(path, manifest.model_dump_json(indent=2) + "\n")

    def save_attempt(self, attempt: RawGenerationAttempt) -> None:
        safe_stage = attempt.stage.replace(":", "-")
        self._write(
            self.root / "raw-model-output" / f"{safe_stage}-{attempt.attempt:02d}.json",
            attempt.model_dump_json(indent=2) + "\n",
        )

    def save_plan(self, plan: MeditationPlan) -> None:
        self._write(self.root / "plan.json", plan.model_dump_json(indent=2) + "\n")

    def load_plan(self) -> MeditationPlan | None:
        path = self.root / "plan.json"
        if not path.exists():
            return None
        try:
            return MeditationPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise WorkspaceError(f"Invalid validated plan checkpoint: {error}") from error

    def save_section(self, section: DraftedSection) -> None:
        self._write(
            self.root / "sections" / f"{section.section_id}.json",
            section.model_dump_json(indent=2) + "\n",
        )

    def load_section(self, planned: PlannedSection) -> DraftedSection | None:
        path = self.root / "sections" / f"{planned.id}.json"
        if not path.exists():
            return None
        try:
            section = DraftedSection.model_validate_json(path.read_text(encoding="utf-8"))
            if section.section_id != planned.id:
                raise ValueError("saved section ID does not match its plan")
            if not planned.minimum_words <= section.word_count <= planned.maximum_words:
                raise ValueError("saved section no longer satisfies its word budget")
            validate_meditation_text(section.text)
            return section
        except (OSError, ValidationError, ValueError) as error:
            raise WorkspaceError(f"Invalid section checkpoint {path}: {error}") from error

    def save_outputs(self, script: str, timeline: Timeline) -> None:
        self._write(self.root / "script.md", script)
        self._write(
            self.root / "timeline.json",
            timeline.model_dump_json(indent=2) + "\n",
        )
