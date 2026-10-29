"""Stub receipts only: rendering, link containment, escaping and truthful labels."""

import hashlib
import json
import tempfile
import unittest
import wave
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from scripts import listening_report as report


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def wav(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0" * 240)


def receipt(audio: Path, *, changes: int = 0) -> dict[str, Any]:
    return {
        "status": "pass",
        "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "expected_transcript": "<script>alert('expected')</script> sound.",
        "asr": {"text": "<script>alert('observed')</script> sound."},
        "failure_reasons": [],
        "review_reasons": [],
        "content": {
            "expected_word_count": 2,
            "observed_word_count": 2,
            "substitutions": changes,
            "insertions": 0,
            "deletions": 0,
            "alignment": [
                {
                    "operation": "substitute" if changes else "match",
                    "expected_index": 0,
                    "observed_index": 0,
                    "expected": "<script>",
                    "observed": "<img onerror='bad'>",
                }
            ],
        },
    }


def fixture(root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    shared = root / "shared accepted script.md"
    shared.write_text("This is stub script content, not model output.")
    for index, candidate in enumerate(
        ("writer-Ryan", 'Aiden <script>"&', "failed-Serena", "unscreened")
    ):
        attempt = root / f"attempt with spaces {index}"
        run = attempt / "production-runs" / f"run-{index}"
        run.mkdir(parents=True)
        write_json(
            run / "run.json",
            {
                "status": "failed" if index == 2 else "completed",
                "source_kind": "generated_prompt" if index == 0 else "script_file",
            },
        )
        (run / "script.md").write_text("Stub.")
        generation = attempt / "generation"
        generation.mkdir()
        for name in ("stdout.log", "stderr.log", "resources.jsonl", "receipt.json"):
            (generation / name).write_text("Stub evidence.")
        record: dict[str, Any] = {
            "case": "grounding <script>alert('case')</script>",
            "candidate": candidate,
            "run_id": f"run-{index}",
            "attempt": str(attempt),
            "status": "fail" if index == 2 else "pass",
            "mode": "prompt_to_audio" if index == 0 else "fixed_script_voice_comparison",
            "comparison_script": str(shared) if index else None,
            "accepted_script": str(shared),
            "actual_seconds": 180.123,
            "target_seconds": 180,
            "resolved_config": {
                "profile": "high" if index == 0 else "basic",
                "tts": {"voice_name": candidate, "backend": "fixture-only"},
            },
            "model_metadata": {
                "tts": {"versioned_model_id": "fixture <script>alert('model')</script>"}
            },
            "generation": {
                "wall_seconds": 1.25,
                "command": ["stub-only", "--script-file", str(shared)]
                if index
                else ["stub-only", "<script>prompt</script>"],
            },
        }
        if index < 2:
            wav(run / "narration.wav")
            result = receipt(run / "narration.wav", changes=index)
            record["final_verification"] = result
            write_json(attempt / "verify-final.json", result)
        if index == 2:
            record["error"] = "Retained <script>alert('failure')</script>"
            native = run / "native-speech" / "failed-attempt"
            native.mkdir(parents=True)
            wav(native / "native.wav")
            write_json(
                native / "metadata.json",
                {
                    "segment_id": "speech-0001",
                    "attempt_id": "failed-native-stub",
                    "eos_observed": False,
                    "wav_path": str(native / "native.wav"),
                },
            )
        records.append(record)
    manifest = {
        "status": "screened_pending_human_listening",
        "records": records,
        "plan": {
            "prompt": "<script>alert('plan')</script>",
            "writing": {"seed": 42, "profile": "high"},
        },
        "skipped": [{"case": "sleep", "reason": "bounded quality failure"}],
    }
    write_json(root / "manifest.json", manifest)
    return manifest


def selected_native(root: Path, manifest: dict[str, Any]) -> tuple[Path, Path]:
    record = manifest["records"][0]
    run = Path(record["attempt"]) / "production-runs" / record["run_id"]
    native = run / "native-speech" / "selected-attempt"
    native.mkdir(parents=True)
    audio = native / "native.wav"
    wav(audio)
    sidecar = native / "metadata.json"
    write_json(
        sidecar,
        {
            "segment_id": "speech-0001",
            "attempt_id": "selected-native-stub",
            "eos_observed": True,
            "wav_path": str(audio),
        },
    )
    verification = receipt(audio)
    record["native_verification"] = [
        {
            "segment_id": "speech-0001",
            "metadata": str(sidecar),
            "verification": verification,
        }
    ]
    write_json(root / "manifest.json", manifest)
    return audio, sidecar


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: list[str] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        for key, value in attrs:
            if value is None:
                continue
            if key in {"href", "src"}:
                self.links.append(value)
            if key == "id":
                self.ids.append(value)


class RenderChecks(unittest.TestCase):
    def test_all_artifact_links_resolve_inside_campaign_and_escape_model_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture(root)
            destination = report.build(root)
            main = destination.read_text(encoding="utf-8")
            self.assertIn("Prompt → audio", main)
            self.assertIn("Fixed-script voice comparison", main)
            self.assertIn("Shared accepted script", main)
            self.assertIn("180.123 seconds", main)
            self.assertIn("Automated speech screening: Pass", main)
            self.assertIn("Automated speech screening: Review", main)
            self.assertIn("Automated speech screening: Fail", main)
            self.assertIn("Automated speech screening: Not screened", main)
            self.assertIn("Human quality: Unrated", main)
            self.assertIn("failed-native-stub", main)
            self.assertIn("Exact normalized word diff", main)
            self.assertIn("&lt;script&gt;", main)
            for page in root.rglob("*.html"):
                parsed = Links()
                parsed.feed(page.read_text(encoding="utf-8"))
                self.assertNotIn("script", parsed.tags)
                self.assertNotIn("img", parsed.tags)
                for value in parsed.links:
                    parts = urlsplit(value)
                    self.assertFalse(parts.scheme)
                    self.assertFalse(parts.netloc)
                    if parts.fragment and not parts.path:
                        self.assertIn(parts.fragment, parsed.ids)
                        continue
                    target = (page.parent / unquote(parts.path)).resolve(strict=True)
                    target.relative_to(root)

    def test_absent_asr_cannot_be_reported_as_pass(self) -> None:
        self.assertEqual(report.status_for("pass", []), "Not screened")
        self.assertEqual(report.status_for("pass", [{"status": "pass"}]), "Not screened")

    def test_word_edits_always_require_review_and_failures_stay_fail(self) -> None:
        evidence: dict[str, Any] = {
            "status": "pass",
            "asr": {"text": "hello"},
            "content": {"expected_word_count": 1, "deletions": 1},
        }
        self.assertEqual(report.status_for("pass", [evidence]), "Review")
        self.assertEqual(report.status_for("fail", [evidence]), "Fail")
        evidence["content"]["possible_truncation"] = True
        self.assertEqual(report.status_for("pass", [evidence]), "Fail")

    def test_absolute_and_symlink_escapes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "campaign"
            root.mkdir()
            outside = parent / "outside.txt"
            outside.write_text("Not a campaign artifact")
            values: list[str | Path] = [outside, "../outside.txt"]
            try:
                (root / "escape.txt").symlink_to(outside)
                values.append("escape.txt")
            except OSError:
                pass  # Windows may require developer mode for symlink creation.
            for value in values:
                with self.assertRaises(ValueError):
                    report.safe_path(root.resolve(), value)
                self.assertNotIn("href=", report.link(root.resolve(), value, "outside"))

    def test_final_audio_changed_after_asr_is_visible_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = fixture(root)
            audio = Path(data["records"][0]["attempt"]) / "production-runs/run-0/narration.wav"
            with audio.open("ab") as stream:
                stream.write(b"changed")
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn("Final WAV hash differs", text)
            self.assertIn('sample-000" class="fail"', text)

    def test_selected_native_with_matching_asr_hash_stays_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected_native(root, fixture(root))
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="pass"', text)
            self.assertIn("Native attempt speech-0001: Pass", text)

    def test_changed_native_audio_fails_native_and_overall_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _ = selected_native(root, fixture(root))
            with audio.open("ab") as stream:
                stream.write(b"changed after native ASR")
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="fail"', text)
            self.assertIn("Native attempt speech-0001: Fail:", text)
            self.assertIn("Native WAV hash differs from retained ASR receipt", text)
            diff = next(root.glob("review-details-*/sample-000-native-000.html")).read_text(
                encoding="utf-8"
            )
            self.assertIn("Screening: <strong>Fail</strong>", diff)
            self.assertIn("Native WAV hash differs", diff)

    def test_missing_native_audio_fails_native_and_overall_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, _ = selected_native(root, fixture(root))
            audio.unlink()
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="fail"', text)
            self.assertIn("Native attempt speech-0001: Fail:", text)
            self.assertIn("Native WAV is missing, unavailable, or outside this campaign", text)
            self.assertNotIn(report.url(root.resolve(), audio.resolve()), text)

    def test_escaping_native_audio_fails_without_linking_outside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "campaign"
            root.mkdir()
            audio, sidecar = selected_native(root, fixture(root))
            outside = parent / "outside.wav"
            wav(outside)
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            metadata["wav_path"] = str(outside)
            write_json(sidecar, metadata)
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="fail"', text)
            self.assertIn("Native attempt speech-0001: Fail:", text)
            self.assertNotIn('src="../outside.wav"', text)
            self.assertNotIn('src="' + str(outside), text)
            self.assertNotIn(report.url(root.resolve(), audio.resolve()), text)

    def test_missing_native_metadata_fails_selected_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, sidecar = selected_native(root, fixture(root))
            sidecar.unlink()
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="fail"', text)
            self.assertIn("Selected native metadata is missing or outside this campaign", text)

    def test_native_asr_without_audio_hash_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            selected_native(root, manifest)
            del manifest["records"][0]["native_verification"][0]["verification"]["audio_sha256"]
            write_json(root / "manifest.json", manifest)
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="fail"', text)
            self.assertIn("Native WAV ASR receipt has no audio SHA256", text)

    def test_missing_native_asr_receipt_cannot_keep_overall_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            selected_native(root, manifest)
            manifest["records"][0]["native_verification"][0]["verification"] = {}
            write_json(root / "manifest.json", manifest)
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="fail"', text)
            self.assertIn("Selected native ASR receipt is missing", text)

    def test_missing_final_audio_cannot_keep_historical_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            run = Path(manifest["records"][0]["attempt"]) / "production-runs/run-0"
            (run / "narration.wav").unlink()
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="fail"', text)
            self.assertIn("Final WAV is missing, unavailable, or outside this campaign", text)
            diff = next(root.glob("review-details-*/sample-000-final.html")).read_text(
                encoding="utf-8"
            )
            self.assertIn("Screening: <strong>Fail</strong>", diff)

    def test_final_asr_without_audio_hash_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            del manifest["records"][0]["final_verification"]["audio_sha256"]
            write_json(root / "manifest.json", manifest)
            text = report.build(root).read_text(encoding="utf-8")
            self.assertIn('sample-000" class="fail"', text)
            self.assertIn("Final WAV ASR receipt has no audio SHA256", text)

    def test_existing_report_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture(root)
            destination = report.build(root)
            original = destination.read_bytes()
            with self.assertRaises(FileExistsError):
                report.build(root)
            self.assertEqual(destination.read_bytes(), original)
            self.assertTrue(report.build(root, output=Path("review-second.html")).is_file())


if __name__ == "__main__":
    unittest.main()
