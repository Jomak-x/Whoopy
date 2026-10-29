"""Immutable file storage for authoritative Evaluation Lab evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from whoopy.evaluation.models import (
    ExperimentDefinition,
    ExperimentReport,
    HumanRating,
    PromotionDecision,
)


class EvaluationStorageError(RuntimeError):
    """Raised when Lab evidence cannot be read or would be overwritten."""


class EvaluationRepository:
    """Persist reports and decisions as inspectable JSON outside rebuildable SQLite."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _validate_experiment_key(experiment_id: str, version: int) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", experiment_id) is None:
            raise EvaluationStorageError("Invalid Lab experiment ID.")
        if isinstance(version, bool) or version < 1:
            raise EvaluationStorageError("Invalid Lab experiment version.")

    def _experiment_root(self, experiment_id: str, version: int) -> Path:
        self._validate_experiment_key(experiment_id, version)
        path = self.root / "experiments" / experiment_id / f"v{version}"
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as error:
            raise EvaluationStorageError("Lab evidence path escapes its managed root.") from error
        return path

    def save_report(self, report: ExperimentReport) -> Path:
        path = (
            self._experiment_root(report.definition.experiment_id, report.definition.version)
            / "report.json"
        )
        self._write_new(path, report.model_dump_json(indent=2) + "\n")
        return path

    def save_definition(self, definition: ExperimentDefinition) -> Path:
        path = (
            self._experiment_root(definition.experiment_id, definition.version) / "definition.json"
        )
        self._write_new(path, definition.model_dump_json(indent=2) + "\n")
        return path

    def load_definition(self, *, experiment_id: str, version: int) -> ExperimentDefinition:
        path = self._experiment_root(experiment_id, version) / "definition.json"
        try:
            return ExperimentDefinition.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            message = f"Could not load experiment definition {path}: {error}"
            raise EvaluationStorageError(message) from error

    def definitions(self) -> list[ExperimentDefinition]:
        values: list[ExperimentDefinition] = []
        for path in sorted((self.root / "experiments").glob("*/v*/definition.json")):
            try:
                values.append(ExperimentDefinition.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValidationError) as error:
                message = f"Could not load Lab definition {path}: {error}"
                raise EvaluationStorageError(message) from error
        return values

    def load_report(self, *, experiment_id: str, version: int) -> ExperimentReport:
        path = self.report_path(experiment_id=experiment_id, version=version)
        try:
            return ExperimentReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            message = f"Could not load experiment report {path}: {error}"
            raise EvaluationStorageError(message) from error

    def has_report(self, *, experiment_id: str, version: int) -> bool:
        return self.report_path(experiment_id=experiment_id, version=version).is_file()

    def report_path(self, *, experiment_id: str, version: int) -> Path:
        """Return the authoritative report path used by hash-bound decisions."""

        return self._experiment_root(experiment_id, version) / "report.json"

    def report_sha256(self, *, experiment_id: str, version: int) -> str:
        """Hash the exact durable report bytes rather than a reserialization."""

        path = self.report_path(experiment_id=experiment_id, version=version)
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            message = f"Could not hash experiment report {path}: {error}"
            raise EvaluationStorageError(message) from error

    def save_execution_control(
        self,
        *,
        experiment_id: str,
        version: int,
        payload: dict[str, Any],
    ) -> Path:
        """Atomically replace mutable batch control state.

        Definitions, reports, ratings, and decisions stay immutable. This one
        file is deliberately mutable because pause/cancel/resume must survive
        an application restart without manufacturing a partial report.
        """

        path = self._experiment_root(experiment_id, version) / "execution-control.json"
        self._replace(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return path

    def load_execution_control(self, *, experiment_id: str, version: int) -> dict[str, Any] | None:
        path = self._experiment_root(experiment_id, version) / "execution-control.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            message = f"Could not load Lab execution state {path}: {error}"
            raise EvaluationStorageError(message) from error
        if not isinstance(value, dict):
            raise EvaluationStorageError(f"Lab execution state must be a JSON object: {path}")
        return value

    def save_decision(self, decision: PromotionDecision) -> Path:
        path = self.root / "decisions" / f"{decision.decision_id}.json"
        self._write_new(path, decision.model_dump_json(indent=2) + "\n")
        return path

    def decisions(self) -> list[PromotionDecision]:
        """Load immutable promotion decisions for disposable projections."""

        values: list[PromotionDecision] = []
        for path in sorted((self.root / "decisions").glob("*.json")):
            try:
                values.append(PromotionDecision.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValidationError) as error:
                message = f"Could not load Lab decision {path}: {error}"
                raise EvaluationStorageError(message) from error
        return values

    def save_rating(
        self,
        *,
        experiment_id: str,
        experiment_version: int,
        rating: HumanRating,
    ) -> Path:
        path = (
            self._experiment_root(experiment_id, experiment_version)
            / "ratings"
            / f"{rating.assignment_id}-{rating.dimension}.json"
        )
        self._write_new(path, rating.model_dump_json(indent=2) + "\n")
        return path

    def ratings(self, *, experiment_id: str, experiment_version: int) -> list[HumanRating]:
        root = self._experiment_root(experiment_id, experiment_version) / "ratings"
        values: list[HumanRating] = []
        for path in sorted(root.glob("*.json")):
            try:
                values.append(HumanRating.model_validate_json(path.read_text("utf-8")))
            except (OSError, ValidationError) as error:
                message = f"Could not load Lab rating {path}: {error}"
                raise EvaluationStorageError(message) from error
        return values

    def _write_new(self, path: Path, payload: str) -> None:
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as error:
            raise EvaluationStorageError("Lab evidence path escapes its managed root.") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise EvaluationStorageError(f"Refusing to replace immutable Lab evidence: {path}")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            # Flush the file itself before the atomic directory entry change.
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            # A hard-link commit is atomic and, unlike Path.replace(), can never
            # overwrite evidence won by a concurrent request.
            os.link(temporary, path)
            temporary.unlink()
        except FileExistsError as error:
            temporary.unlink(missing_ok=True)
            raise EvaluationStorageError(
                f"Refusing to replace immutable Lab evidence: {path}"
            ) from error
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise EvaluationStorageError(f"Could not save Lab evidence {path}: {error}") from error

    def _replace(self, path: Path, payload: str) -> None:
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError as error:
            raise EvaluationStorageError("Lab evidence path escapes its managed root.") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            temporary.replace(path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            message = f"Could not save Lab execution state {path}: {error}"
            raise EvaluationStorageError(message) from error
