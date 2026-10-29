from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_returned_native_arrays_are_excluded_from_publication() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        "evaluations/benchmarks/disposable/native/returned-0000.npy",
        ".whoopy-recovery/disposable/returned-0000.npy",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=root,
        input="\n".join([*paths, "src/whoopy/cli.py"]) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.splitlines() == paths


def _assert_compact_evidence(value: object) -> None:
    if isinstance(value, dict):
        forbidden = {
            "asr_text",
            "existing_asr_transcript",
            "alignment",
            "normalized_word_differences",
            "aggregate_normalized_word_differences",
            "missing_words",
            "unexpected_words",
            "missing_tail_words",
            "excess_repeated_words",
        }
        assert not forbidden.intersection(value)
        asr = value.get("asr")
        if isinstance(asr, dict):
            assert not {"text", "segments", "tokens", "words"}.intersection(asr)
        for child in value.values():
            _assert_compact_evidence(child)
    elif isinstance(value, list):
        for child in value:
            _assert_compact_evidence(child)


def test_published_evaluation_json_excludes_full_asr_traces() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "ls-files", "-z", "evaluations/**/*.json"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = [root / name for name in result.stdout.split("\0") if name]
    assert paths
    compact_receipts = []
    for path in paths:
        receipt = json.loads(path.read_text())
        _assert_compact_evidence(receipt)
        if "asr" in receipt or "original_metrics" in receipt:
            publication = receipt["publication"]
            assert publication["schema_version"] == 1
            assert publication["kind"] == "compact_verification_receipt"
            assert publication["full_trace_included"] is False
            assert publication["original_status"] == receipt["status"]
            assert publication["referenced_receipt_hashes"] == "original_receipt_bytes"
            digest = publication["original_receipt_sha256"]
            assert len(digest) == 64
            assert bytes.fromhex(digest)
            compact_receipts.append(path)
    assert len(compact_receipts) == 37


def test_compact_vox_receipts_preserve_disagreement_and_assembly_proof() -> None:
    root = Path(__file__).resolve().parents[1] / "evaluations/benchmarks/2026-10-28/verification"
    full_file = json.loads((root / "voxcpm2-designed-male.json").read_text())
    aggregate = json.loads((root / "voxcpm2-designed-male-aggregate.json").read_text())
    strict = json.loads((root / "policy-v3/voxcpm2-designed-male-strict-review.json").read_text())
    assert full_file["status"] == "fail"
    assert aggregate["status"] == "pass"
    assert aggregate["full_file_asr_status"] == "fail"
    assert aggregate["assembly_proof"]["equal"] is True
    assert aggregate["assembly_proof"]["inserted_frames"] == 0
    assert len(aggregate["segments"]) == 4
    for segment in aggregate["segments"]:
        assert segment["status"] == "pass"
        report = json.loads((root.parents[3] / segment["report"]).read_text())
        assert segment["report_sha256"] == report["publication"]["original_receipt_sha256"]
    assert strict["status"] == "review"
    assert strict["normalized_word_edit_count"] == 28
    assert strict["reassessment_does_not_resolve_asr_disagreement"] is True
