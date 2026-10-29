#!/usr/bin/env python3
"""Build an inspectable Meditation Engine V2 automatic corpus receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from whoopy.evaluation import load_evaluation_set
from whoopy.evaluation.models import BakeoffReport
from whoopy.meditation import load_blueprint_library, review_script
from whoopy.meditation.models import DraftedSection, MeditationPlan
from whoopy.meditation.pacing import MAX_SENTENCE_WORDS, sentence_word_count, split_sentences
from whoopy.meditation.safety import ContentSafetyError, validate_meditation_text
from whoopy.timeline import BreathSegment, SilenceSegment, SpeechSegment, Timeline

BEAT_SEQUENCES: dict[int, tuple[str, ...]] = {
    3: ("arrival", "practice", "ending"),
    4: ("arrival", "instruction", "deeper_practice", "ending"),
    5: ("arrival", "instruction", "practice", "reminder", "ending"),
    6: ("arrival", "instruction", "practice", "reminder", "deeper_practice", "ending"),
}
TIMED_BREATH = re.compile(
    r"\b(?:inhale|exhale|breathe in|breathe out)\b.{0,30}\b(?:seconds?|counts?)\b",
    re.I,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _issue(code: str, message: str, *, section_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if section_id is not None:
        payload["section_id"] = section_id
    return payload


def inspect_case(
    *,
    case: Any,
    result: Any,
    artifact_root: Path,
) -> dict[str, Any]:
    case_root = artifact_root / result.profile / case.id
    issues: list[dict[str, Any]] = []
    artifact_paths = {
        "plan": case_root / "plan.json",
        "script": case_root / "script.md",
        "timeline": case_root / "timeline.json",
    }
    if not result.success:
        issues.append(_issue("production_failure", result.failure or "Corpus generation failed."))
    missing = [name for name, path in artifact_paths.items() if not path.is_file()]
    if missing:
        issues.append(_issue("missing_artifact", "Missing artifacts: " + ", ".join(missing)))
    if issues:
        return {
            "case_id": case.id,
            "automatic_passed": False,
            "issues": issues,
            "artifacts": {},
        }

    plan = MeditationPlan.model_validate_json(artifact_paths["plan"].read_text("utf-8"))
    timeline = Timeline.model_validate_json(artifact_paths["timeline"].read_text("utf-8"))
    sections = [
        DraftedSection.model_validate_json(
            case_root.joinpath("sections", f"{planned.id}.json").read_text("utf-8")
        )
        for planned in plan.sections
    ]
    blueprint = load_blueprint_library().for_kind(plan.meditation_kind)

    if plan.blueprint_id != case.blueprint_id:
        issues.append(
            _issue(
                "blueprint_mismatch",
                f"Expected {case.blueprint_id}, generated {plan.blueprint_id}.",
            )
        )
    if plan.meditation_kind != case.category:
        issues.append(
            _issue(
                "kind_mismatch",
                f"Expected {case.category}, generated {plan.meditation_kind}.",
            )
        )
    expected_beats = BEAT_SEQUENCES[len(plan.sections)]
    actual_beats = tuple(section.beat for section in plan.sections)
    if actual_beats != expected_beats:
        issues.append(
            _issue(
                "progression_mismatch",
                f"Expected beat progression {expected_beats}, generated {actual_beats}.",
            )
        )
    for planned in plan.sections:
        if planned.technique not in blueprint.allowed_techniques:
            issues.append(
                _issue(
                    "unsupported_technique",
                    f"{planned.technique} is outside {blueprint.versioned_id}.",
                    section_id=planned.id,
                )
            )
    expected_ending = "sleep_transition" if plan.meditation_kind == "sleep" else "return"
    if plan.sections[-1].technique != expected_ending:
        issues.append(
            _issue(
                "ending_mismatch",
                f"Expected {expected_ending}, generated {plan.sections[-1].technique}.",
                section_id=plan.sections[-1].id,
            )
        )

    editorial = review_script(plan=plan, sections=sections)
    for editorial_issue in editorial.issues:
        for section_id in editorial_issue.section_ids:
            issues.append(
                _issue(
                    editorial_issue.code,
                    editorial_issue.message,
                    section_id=section_id,
                )
            )
    for section in sections:
        try:
            validate_meditation_text(section.text)
        except ContentSafetyError as error:
            issues.append(_issue("safety_failure", str(error), section_id=section.section_id))
        for sentence in split_sentences(section.text):
            words = sentence_word_count(sentence)
            if words > MAX_SENTENCE_WORDS:
                issues.append(
                    _issue(
                        "overlong_sentence",
                        f"{words} words: {sentence}",
                        section_id=section.section_id,
                    )
                )
        if plan.breath_mode == "natural_awareness" and TIMED_BREATH.search(section.text):
            issues.append(
                _issue(
                    "invented_breath_timing",
                    "Natural breath awareness contains model-authored timing.",
                    section_id=section.section_id,
                )
            )
    if plan.breath_mode == "natural_awareness" and any(
        isinstance(segment, BreathSegment) for segment in timeline.segments
    ):
        issues.append(
            _issue("unexpected_breath_cycle", "Natural awareness compiled a timed breath cycle.")
        )

    speech = [segment for segment in timeline.segments if isinstance(segment, SpeechSegment)]
    silence = [segment for segment in timeline.segments if isinstance(segment, SilenceSegment)]
    longest_speech_words = max((sentence_word_count(segment.text) for segment in speech), default=0)
    if longest_speech_words > 24:
        issues.append(
            _issue(
                "long_uninterrupted_speech",
                f"A timeline speech block contains {longest_speech_words} words; maximum is 24.",
            )
        )
    if not silence:
        issues.append(_issue("missing_quiet", "Timeline contains no deliberate silence."))
    elif max(segment.duration_ms for segment in silence) > 120_000:
        issues.append(_issue("oversized_quiet", "A deliberate quiet block exceeds 120 seconds."))
    if case.duration_seconds >= 180 and not any(
        segment.duration_ms >= 6_000 for segment in silence
    ):
        issues.append(
            _issue("missing_practice_quiet", "No quiet practice block reaches six seconds.")
        )
    if plan.ending_behavior == "daytime_reorientation":
        ending_pause_ms = plan.sections[-1].pause_after_ms
        practice_pause_ms = max(section.pause_after_ms for section in plan.sections[:-1])
        if ending_pause_ms >= practice_pause_ms:
            issues.append(
                _issue(
                    "ending_quieter_than_practice",
                    "Daytime ending quiet is not shorter than the main practice quiet.",
                    section_id=plan.sections[-1].id,
                )
            )

    timing_error = abs(result.estimated_duration_seconds - case.duration_seconds)
    timing_error_percent = timing_error / case.duration_seconds * 100
    if timing_error_percent > 5:
        issues.append(
            _issue(
                "duration_out_of_range",
                f"Estimated duration error is {timing_error_percent:.3f}%; maximum is 5%.",
            )
        )
    return {
        "case_id": case.id,
        "automatic_passed": not issues,
        "issues": issues,
        "metrics": {
            "estimated_duration_seconds": result.estimated_duration_seconds,
            "timing_error_percent": round(timing_error_percent, 4),
            "validation_retries": result.validation_retries,
            "repeated_trigram_ratio": result.repeated_trigram_ratio,
            "section_count": len(plan.sections),
            "speech_segment_count": len(speech),
            "silence_segment_count": len(silence),
            "longest_speech_words": longest_speech_words,
            "longest_silence_ms": max((segment.duration_ms for segment in silence), default=0),
            "final_section_pause_ms": plan.sections[-1].pause_after_ms,
            "main_practice_pause_ms": max(
                (section.pause_after_ms for section in plan.sections[:-1]), default=0
            ),
        },
        "artifacts": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in artifact_paths.items()
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bakeoff-report", type=Path, required=True)
    parser.add_argument(
        "--evaluation-set", type=Path, default=Path("config/evaluation/local-v1.yaml")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    evaluation = load_evaluation_set(args.evaluation_set)
    report = BakeoffReport.model_validate_json(args.bakeoff_report.read_text("utf-8"))
    if (
        report.evaluation_id != evaluation.evaluation_id
        or report.evaluation_version != evaluation.version
    ):
        parser.error("bake-off report does not match the requested evaluation set revision")
    by_case = {result.case_id: result for result in report.results}
    cases = [
        inspect_case(
            case=case,
            result=by_case[case.id],
            artifact_root=args.bakeoff_report.parent / "artifacts",
        )
        for case in evaluation.cases
    ]
    passed = sum(case["automatic_passed"] for case in cases)
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_version": evaluation.version,
        "candidate_profiles": sorted(report.candidates),
        "writing_method": report.writing_method,
        "seed": report.seed,
        "platform": report.platform,
        "source_report": {
            "path": str(args.bakeoff_report),
            "sha256": _sha256(args.bakeoff_report),
        },
        "expected_case_count": len(evaluation.cases),
        "automatic_pass_count": passed,
        "no_safety_failures": not any(
            issue["code"] == "safety_failure" for case in cases for issue in case["issues"]
        ),
        "automatic_accepted": passed == len(evaluation.cases),
        "human_review_status": "pending",
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0 if payload["automatic_accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
