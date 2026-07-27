"""Measure generation candidates while retaining per-case artifacts."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import psutil

from whoopy.evaluation.models import (
    BakeoffCaseResult,
    BakeoffReport,
    EvaluationSet,
)
from whoopy.meditation import (
    GenerationWorkspace,
    LocalMeditationGenerator,
    PromptBundle,
)
from whoopy.meditation.models import RawGenerationAttempt
from whoopy.ports import ScriptGenerator

WORD_PATTERN = re.compile(r"\b[\w'-]+\b")
INVITATIONAL_PATTERN = re.compile(
    r"\b(?:you might|you may|if (?:it|that) feels comfortable|when you are ready)\b",
    re.I,
)


class _PeakProcessTree:
    """Sample this Python process plus model child processes at short intervals."""

    def __init__(self) -> None:
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        process = psutil.Process()
        while not self._stop.wait(0.02):
            processes = [process, *process.children(recursive=True)]
            total = 0
            for current in processes:
                try:
                    total += current.memory_info().rss
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
            self.peak_bytes = max(self.peak_bytes, total)

    def __enter__(self) -> _PeakProcessTree:
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


def _repeated_trigram_ratio(text: str) -> float:
    words = [word.casefold() for word in WORD_PATTERN.findall(text)]
    trigrams = [tuple(words[index : index + 3]) for index in range(len(words) - 2)]
    if not trigrams:
        return 0.0
    return round((len(trigrams) - len(set(trigrams))) / len(trigrams), 4)


def _saved_validation_retries(workspace: GenerationWorkspace) -> int:
    retries = 0
    for path in (workspace.root / "raw-model-output").glob("*.json"):
        try:
            attempt = RawGenerationAttempt.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        retries += attempt.validation_error is not None
    return retries


class BakeoffRunner:
    """Run the same validated workflow for each candidate and keep tradeoffs."""

    def __init__(
        self,
        *,
        evaluation_set: EvaluationSet,
        prompts: PromptBundle,
        output_directory: Path,
        seed: int = 42,
    ) -> None:
        self.evaluation_set = evaluation_set
        self.prompts = prompts
        self.output_directory = output_directory
        self.seed = seed

    def run_candidate(
        self,
        *,
        profile: Literal["lite", "standard"],
        adapter: ScriptGenerator,
        model_artifact_bytes: int,
    ) -> list[BakeoffCaseResult]:
        if profile not in ("lite", "standard"):
            raise ValueError("bake-off profile must be lite or standard")
        results: list[BakeoffCaseResult] = []
        for case in self.evaluation_set.cases:
            run_id = uuid5(
                NAMESPACE_URL,
                f"{self.evaluation_set.evaluation_id}:{self.evaluation_set.version}:"
                f"{profile}:{case.id}:{self.seed}",
            )
            workspace = GenerationWorkspace(self.output_directory / "artifacts" / profile / case.id)
            started = time.monotonic()
            with _PeakProcessTree() as memory:
                try:
                    generated = LocalMeditationGenerator(
                        adapter,
                        self.prompts,
                        workspace=workspace,
                    ).generate(
                        prompt=case.prompt,
                        duration_seconds=case.duration_seconds,
                        run_id=run_id,
                        seed=self.seed,
                    )
                except Exception as error:
                    results.append(
                        BakeoffCaseResult(
                            case_id=case.id,
                            category=case.category,
                            profile=profile,
                            success=False,
                            elapsed_seconds=time.monotonic() - started,
                            peak_process_tree_mb=memory.peak_bytes / 1_048_576,
                            model_artifact_bytes=model_artifact_bytes,
                            validation_retries=_saved_validation_retries(workspace),
                            safety_validation_passed=False,
                            failure=f"{type(error).__name__}: {error}"[:2_000],
                        )
                    )
                    continue
            retries = sum(
                attempt.validation_error is not None for attempt in generated.raw_attempts
            )
            results.append(
                BakeoffCaseResult(
                    case_id=case.id,
                    category=case.category,
                    profile=profile,
                    success=True,
                    elapsed_seconds=time.monotonic() - started,
                    peak_process_tree_mb=memory.peak_bytes / 1_048_576,
                    model_artifact_bytes=model_artifact_bytes,
                    section_count=len(generated.sections),
                    validation_retries=retries,
                    estimated_duration_seconds=generated.estimated_duration_seconds,
                    timing_error_percent=abs(
                        generated.estimated_duration_seconds - case.duration_seconds
                    )
                    / case.duration_seconds
                    * 100,
                    repeated_trigram_ratio=_repeated_trigram_ratio(generated.script),
                    invitational_phrase_count=len(INVITATIONAL_PATTERN.findall(generated.script)),
                    safety_validation_passed=True,
                )
            )
        return results

    def report(
        self,
        *,
        platform: str,
        candidates: Mapping[str, ScriptGenerator],
        results: list[BakeoffCaseResult],
    ) -> BakeoffReport:
        return BakeoffReport(
            evaluation_id=self.evaluation_set.evaluation_id,
            evaluation_version=self.evaluation_set.version,
            created_at=datetime.now(UTC),
            seed=self.seed,
            platform=platform,
            candidates={name: adapter.metadata for name, adapter in candidates.items()},
            results=results,
        )
