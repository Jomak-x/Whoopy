"""Local stub manifests only; no models, inference, GPU, audio decoding or sockets."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from scripts import build_morning_hub as hub


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.tags: list[str] = []
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        for key, value in attrs:
            if key == "href" and value:
                self.urls.append(value)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def rendered_row(parsed: Links, marker: str) -> list[str]:
    return next(row for row in parsed.rows if any(marker in cell for cell in row))


def fixture(root: Path) -> Path:
    campaign = root / "2026-10-29/pilot"
    records = []
    for index, status in enumerate(("pass", "review", "fail")):
        attempt = campaign / f"attempt {index}"
        run = attempt / "production-runs" / f"run-{index}"
        run.mkdir(parents=True)
        audio = run / "narration.wav"
        if status != "fail":
            audio.write_bytes(b"stub; not actual audio")
        records.append(
            {
                "case": "grounding",
                "candidate": '<script>alert("voice")</script>',
                "run_id": f"run-{index}",
                "attempt": str(attempt),
                "status": status,
                "mode": "prompt_to_audio" if index == 0 else "fixed_script_voice_comparison",
                "human_review": "pending",
                "actual_seconds": 180 if status != "fail" else None,
                "error": "<script>failure</script>" if status == "fail" else "",
                "final_verification": {"audio_sha256": sha256(audio)} if status != "fail" else {},
            }
        )
    write(
        campaign / "manifest.json",
        {"status": "screened_pending_human_listening", "records": records},
    )
    (campaign / "review.html").write_text("stub review")
    (campaign / "git-head.txt").write_text("abc123pilot")
    prompt = campaign / "source-snapshot/config/prompts/whole_session_system.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("---\nversion: 5\n---\nStub frozen prompt")
    legacy_output = root / "2026-10-28/old.wav"
    legacy_input = root / "2026-10-28/raw.wav"
    legacy_output.parent.mkdir(parents=True, exist_ok=True)
    legacy_output.write_bytes(b"stub listening copy")
    legacy_input.write_bytes(b"stub raw input")
    write(
        root / "2026-10-28/verification/listening/strict.json",
        {
            "policy_id": "strict-v3",
            "records": [
                {
                    "candidate": "old-voice",
                    "status": "review",
                    "output": "evaluations/benchmarks/2026-10-28/old.wav",
                    "output_sha256": sha256(legacy_output),
                    "input": "evaluations/benchmarks/2026-10-28/raw.wav",
                    "input_sha256": sha256(legacy_input),
                }
            ],
        },
    )
    (root / "2026-10-28/review.html").write_text("stub original page")
    script = root / "2026-10-29/writing/script.md"
    script.parent.mkdir(parents=True)
    script.write_text("Stub prose")
    write(
        root / "2026-10-29/writing/report.json",
        {
            "human_review": "pending",
            "profile": "high",
            "seed": 42,
            "results": [
                {
                    "case": "focus",
                    "writing_method": "recipe_whole_session",
                    "status": "generated_pending_content_review",
                    "words": 100,
                    "script": str(script),
                }
            ],
        },
    )
    catalog = root / "catalog.json"
    write(
        catalog,
        {
            "datasets": [
                {
                    "kind": "legacy",
                    "label": "Oct28",
                    "path": "2026-10-28/verification/listening/strict.json",
                    "review": "2026-10-28/review.html",
                },
                {"kind": "campaign", "label": "Pilot", "path": "2026-10-29/pilot/manifest.json"},
                {"kind": "campaign", "label": "Final v6", "path": None},
                {
                    "kind": "writing",
                    "label": "Fresh writing",
                    "path": "2026-10-29/writing/report.json",
                    "source_commit": "writing-commit",
                },
            ]
        },
    )
    return catalog


class HubChecks(unittest.TestCase):
    def test_per_scope_counts_source_versions_escaping_and_all_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            catalog = fixture(root)
            output = hub.build(root, catalog, root / "morning.html", "http://127.0.0.1:8772/")
            text = output.read_text()
            self.assertIn("3 attempt records; 2 current final audio bindings pass", text)
            self.assertIn("1 Pass, 1 Review, 1 Fail, 0 Not screened", text)
            self.assertIn("Historical saved screening", text)
            self.assertIn("Current playback integrity", text)
            self.assertIn("Final WAV matches the retained ASR audio SHA256", text)
            self.assertIn("Listening copy and every linked original match", text)
            self.assertIn("abc123pilot", text)
            self.assertIn("whole-session prompt v5", text)
            self.assertIn("writing-commit", text)
            self.assertIn("no speech Pass/Review/Fail total is assigned", text)
            self.assertIn("Dataset path has not been supplied", text)
            self.assertIn("&lt;script&gt;", text)
            self.assertIn("pending; no score supplied", text)
            parsed = Links()
            parsed.feed(text)
            self.assertNotIn("script", parsed.tags)
            for url in parsed.urls:
                parts = urlsplit(url)
                self.assertEqual(parts.scheme, "http")
                self.assertEqual(parts.netloc, "127.0.0.1:8772")
                (root / unquote(parts.path).lstrip("/")).resolve(strict=True).relative_to(root)

    def test_campaign_audio_tamper_removal_and_unbound_receipt_fail_current_integrity(self) -> None:
        cases = ("changed", "missing", "unbound")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                catalog = fixture(root)
                manifest_path = root / "2026-10-29/pilot/manifest.json"
                manifest = json.loads(manifest_path.read_text())
                record = manifest["records"][0]
                audio = (
                    Path(record["attempt"]) / "production-runs" / record["run_id"] / "narration.wav"
                )
                if case == "changed":
                    audio.write_bytes(audio.read_bytes() + b"changed")
                    expected = "Final WAV changed after its retained receipt was written."
                elif case == "missing":
                    audio.unlink()
                    expected = "Final WAV is missing."
                else:
                    del record["final_verification"]["audio_sha256"]
                    write(manifest_path, manifest)
                    expected = (
                        "Final WAV has no valid retained SHA256; current playback is unbound."
                    )
                retained_manifest = manifest_path.read_bytes()
                retained_review = (manifest_path.parent / "review.html").read_bytes()
                output = hub.build(root, catalog, root / "morning.html", "http://127.0.0.1:8772/")
                text = output.read_text()
                self.assertIn(expected, text)
                self.assertIn("Historical saved screening labels", text)
                self.assertIn("1 Pass, 1 Review, 1 Fail, 0 Not screened", text)
                parsed = Links()
                parsed.feed(text)
                affected = rendered_row(parsed, "run-0")
                self.assertEqual(affected[1], "Pass")
                self.assertEqual(affected[2], f"Fail {expected}")
                linked = {unquote(urlsplit(url).path).lstrip("/") for url in parsed.urls}
                relative_audio = audio.relative_to(root).as_posix()
                if case == "missing":
                    self.assertNotIn(relative_audio, linked)
                else:
                    self.assertIn(relative_audio, linked)
                self.assertEqual(manifest_path.read_bytes(), retained_manifest)
                self.assertEqual(
                    (manifest_path.parent / "review.html").read_bytes(), retained_review
                )

    def test_campaign_fallback_final_receipt_binds_audio_then_detects_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            catalog = fixture(root)
            manifest_path = root / "2026-10-29/pilot/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            record = manifest["records"][0]
            retained = record.pop("final_verification")
            write(manifest_path, manifest)
            attempt = Path(record["attempt"])
            receipt_path = attempt / "verify-final.json"
            write(receipt_path, retained)
            audio = attempt / "production-runs" / record["run_id"] / "narration.wav"
            retained_manifest = manifest_path.read_bytes()
            retained_receipt = receipt_path.read_bytes()
            retained_review = (manifest_path.parent / "review.html").read_bytes()

            first = hub.build(root, catalog, root / "morning.html", "http://127.0.0.1:8772/")
            parsed = Links()
            parsed.feed(first.read_text())
            affected = rendered_row(parsed, "run-0")
            self.assertEqual(affected[1], "Pass")
            self.assertTrue(affected[2].startswith("Pass Final WAV matches"))

            audio.write_bytes(audio.read_bytes() + b"changed")
            second = hub.build(
                root, catalog, root / "morning-tampered.html", "http://127.0.0.1:8772/"
            )
            parsed = Links()
            parsed.feed(second.read_text())
            affected = rendered_row(parsed, "run-0")
            self.assertEqual(affected[1], "Pass")
            self.assertEqual(
                affected[2],
                "Fail Final WAV changed after its retained receipt was written.",
            )
            self.assertEqual(manifest_path.read_bytes(), retained_manifest)
            self.assertEqual(receipt_path.read_bytes(), retained_receipt)
            self.assertEqual((manifest_path.parent / "review.html").read_bytes(), retained_review)

    def test_legacy_output_and_linked_input_tamper_removal_and_unbound_hashes_fail(self) -> None:
        for field in ("output", "input"):
            for case in ("changed", "missing", "unbound"):
                with self.subTest(field=field, case=case), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve()
                    catalog = fixture(root)
                    manifest_path = root / "2026-10-28/verification/listening/strict.json"
                    manifest = json.loads(manifest_path.read_text())
                    record = manifest["records"][0]
                    audio = hub.artifact(root, record[field])
                    label = "Listening copy" if field == "output" else "Original raw audio"
                    if case == "changed":
                        audio.write_bytes(audio.read_bytes() + b"changed")
                        expected = f"{label} changed after its retained receipt was written."
                    elif case == "missing":
                        audio.unlink()
                        expected = f"{label} is missing."
                    else:
                        del record[f"{field}_sha256"]
                        write(manifest_path, manifest)
                        expected = (
                            f"{label} has no valid retained SHA256; current playback is unbound."
                        )
                    retained_manifest = manifest_path.read_bytes()
                    retained_review = (root / "2026-10-28/review.html").read_bytes()
                    output = hub.build(
                        root, catalog, root / "morning.html", "http://127.0.0.1:8772/"
                    )
                    text = output.read_text()
                    self.assertIn(expected, text)
                    self.assertIn(
                        "Historical saved screening labels for 1 listed auditions: "
                        "0 Pass, 1 Review, 0 Fail, 0 Not screened",
                        text,
                    )
                    parsed = Links()
                    parsed.feed(text)
                    affected = rendered_row(parsed, "old-voice")
                    self.assertEqual(affected[1], "Review")
                    self.assertEqual(affected[2], f"Fail {expected}")
                    linked = {unquote(urlsplit(url).path).lstrip("/") for url in parsed.urls}
                    relative_audio = audio.relative_to(root).as_posix()
                    if case == "missing":
                        self.assertNotIn(relative_audio, linked)
                    else:
                        self.assertIn(relative_audio, linked)
                    self.assertEqual(manifest_path.read_bytes(), retained_manifest)
                    self.assertEqual(
                        (root / "2026-10-28/review.html").read_bytes(), retained_review
                    )

    def test_outside_and_symlink_artifacts_never_become_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "benchmarks"
            root.mkdir()
            outside = base / "outside.json"
            outside.write_text("{}")
            values: list[str | Path] = [outside, "../outside.json"]
            try:
                (root / "escape.json").symlink_to(outside)
                values.append("escape.json")
            except OSError:
                pass
            for value in values:
                self.assertNotIn(
                    "href=", hub.artifact_link(root, "http://127.0.0.1:8772/", value, "outside")
                )

    def test_requires_local_canonical_server_root_and_exclusive_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = fixture(root)
            for base in (
                "https://example.com/",
                "http://127.0.0.1:8771/",
                "http://127.0.0.1:8772/2026-10-29/",
                "http://user:pass@127.0.0.1:8772/",
            ):
                with self.assertRaises(ValueError):
                    hub.build(root, catalog, root / "bad.html", base)
            output = hub.build(root, catalog, root / "morning.html", "http://127.0.0.1:8772/")
            previous = output.read_bytes()
            with self.assertRaises(FileExistsError):
                hub.build(root, catalog, output, "http://127.0.0.1:8772/")
            self.assertEqual(output.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
