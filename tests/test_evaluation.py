from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from whoopy.evaluation import BakeoffRunner, EvaluationSetError, load_evaluation_set
from whoopy.meditation import load_prompt_bundle
from whoopy.ports import (
    AdapterMetadata,
    ScriptGenerationRequest,
    ScriptGenerationResult,
)


class EvaluationFixtureGenerator:
    metadata = AdapterMetadata(
        adapter_id="test.evaluation",
        versioned_model_id="fixture@1",
        runtime_id="python",
        runtime_version="test",
        license_id="CC0-1.0",
        device="fixture",
    )

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        if "Create the structural JSON plan" in request.prompt:
            value = {
                "title": "A Measured Pause",
                "intention": "Offer an invitational practice.",
                "sections": [
                    {
                        "id": section_id,
                        "title": section_id.title(),
                        "purpose": f"Guide the {section_id} section.",
                        "weight": 1,
                        "pause_seconds": 6,
                    }
                    for section_id in ("arrive", "notice", "return")
                ],
            }
        else:
            section_id = request.prompt.split("Section ID: ", 1)[1].splitlines()[0]
            match = re.search(r"Word range: (\d+)-(\d+)", request.prompt)
            assert match is not None
            word_count = int(match.group(2))
            words = ["You", "might", *([section_id] * (word_count - 2))]
            sentences = [
                " ".join(words[index : index + 10]) + "." for index in range(0, len(words), 10)
            ]
            value = {"section_id": section_id, "text": " ".join(sentences)}
        return ScriptGenerationResult(
            text=json.dumps(value),
            metadata=self.metadata,
            elapsed_seconds=0.01,
        )


def test_versioned_evaluation_set_has_required_categories() -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/phase-3-5.yaml"))

    assert evaluation.version == 1
    assert {case.category for case in evaluation.cases} == {
        "sleep",
        "grounding",
        "anxiety",
        "body_scan",
        "breath_awareness",
        "focus",
    }


def test_invalid_evaluation_set_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: 1\ncases: []\n", encoding="utf-8")

    with pytest.raises(EvaluationSetError):
        load_evaluation_set(path)


def test_bakeoff_keeps_individual_metrics_without_total_score(tmp_path: Path) -> None:
    evaluation = load_evaluation_set(Path("config/evaluation/phase-3-5.yaml"))
    evaluation = evaluation.model_copy(update={"cases": evaluation.cases[:1]})
    adapter = EvaluationFixtureGenerator()
    runner = BakeoffRunner(
        evaluation_set=evaluation,
        prompts=load_prompt_bundle(Path("config/prompts")),
        output_directory=tmp_path,
    )

    results = runner.run_candidate(
        profile="lite",
        adapter=adapter,
        model_artifact_bytes=1_000,
    )
    report = runner.report(
        platform="test-x86_64",
        candidates={"lite": adapter},
        results=results,
    )

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].safety_validation_passed is True
    assert results[0].timing_error_percent is not None
    assert results[0].invitational_phrase_count == 3
    assert report.human_review_status == "pending"
    assert "score" not in report.model_dump()
