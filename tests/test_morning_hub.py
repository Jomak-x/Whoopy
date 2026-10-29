"""Local stub manifests only; no models, inference, GPU, audio decoding or sockets."""

from __future__ import annotations

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


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        for key, value in attrs:
            if key == "href" and value:
                self.urls.append(value)


def fixture(root: Path) -> Path:
    campaign = root / "2026-10-29/pilot"
    records = []
    for index, status in enumerate(("pass", "review", "fail")):
        attempt = campaign / f"attempt {index}"
        run = attempt / "production-runs" / f"run-{index}"
        run.mkdir(parents=True)
        if status != "fail":
            (run / "narration.wav").write_bytes(b"stub; not actual audio")
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
    write(
        root / "2026-10-28/verification/listening/strict.json",
        {
            "policy_id": "strict-v3",
            "records": [
                {
                    "candidate": "old-voice",
                    "status": "review",
                    "output": "evaluations/benchmarks/2026-10-28/old.wav",
                }
            ],
        },
    )
    (root / "2026-10-28/old.wav").write_bytes(b"stub")
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
            self.assertIn("3 attempt records; 2 final audio files present", text)
            self.assertIn("1 Pass, 1 Review, 1 Fail, 0 Not screened", text)
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
