"""Load versioned prompt text without hiding prompt changes in Python code."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PromptDocument(BaseModel):
    """One reviewed prompt with a stable ID and integer version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    text: str = Field(min_length=1)


class PromptBundle(BaseModel):
    """Prompts required by one generation attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: PromptDocument
    section: PromptDocument
    editorial: PromptDocument


class PromptLoadError(ValueError):
    """Raised when a versioned prompt file is malformed."""


def _load_document(path: Path) -> PromptDocument:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise PromptLoadError(f"Could not read prompt {path}: {error}") from error
    if not content.startswith("---\n"):
        raise PromptLoadError(f"Prompt {path} must start with YAML front matter.")
    try:
        _, front_matter, text = content.split("---", 2)
        metadata = yaml.safe_load(front_matter)
        return PromptDocument.model_validate({**metadata, "text": text.strip()})
    except (ValueError, TypeError, ValidationError, yaml.YAMLError) as error:
        raise PromptLoadError(f"Invalid prompt document {path}: {error}") from error


def load_prompt_bundle(directory: Path) -> PromptBundle:
    """Load every prompt used by the local generation workflow."""

    return PromptBundle(
        plan=_load_document(directory / "plan_system.md"),
        section=_load_document(directory / "script_system.md"),
        editorial=_load_document(directory / "editorial_system.md"),
    )
