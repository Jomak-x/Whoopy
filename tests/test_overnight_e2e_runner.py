"""Small orchestration checks. Only ordinary Python subprocesses; no inference."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from scripts import overnight_e2e as runner


def observer(available: int = 32 * 1024**3) -> SimpleNamespace:
    return SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(available=available),
        swap_memory=lambda: SimpleNamespace(used=0),
        Process=lambda pid: SimpleNamespace(
            children=lambda recursive: [], memory_info=lambda: SimpleNamespace(rss=64)
        ),
        NoSuchProcess=ProcessLookupError,
        ZombieProcess=ProcessLookupError,
        AccessDenied=PermissionError,
    )


class RunnerChecks(unittest.TestCase):
    def call(self, root: Path, code: str, **changes: Any) -> dict[str, Any]:
        options: dict[str, Any] = dict(
            cwd=root,
            env={"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if os.name == "nt" else {},
            timeout=5,
            minimum_gib=0,
            deadline=time.monotonic() + 10,
            psutil=observer(),
        )
        options.update(changes)
        with patch.object(runner, "check_idle", lambda: None):
            return runner.bounded([sys.executable, "-c", code], root / "stage", **options)

    def test_success_retains_command_output_samples_and_walltime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.call(root, "print('ordinary stub child')")
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["status"], "completed")
            self.assertIn("ordinary stub child", (root / "stage/stdout.log").read_text())
            self.assertGreaterEqual(result["wall_seconds"], 0)
            self.assertTrue((root / "stage/resources.jsonl").is_file())

    def test_nonzero_keeps_stderr_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.call(
                root, "import sys; print('retained failure', file=sys.stderr); sys.exit(3)"
            )
            self.assertEqual(result["returncode"], 3)
            self.assertEqual(result["status"], "nonzero")
            self.assertIn("retained failure", (root / "stage/stderr.log").read_text())
            self.assertEqual(json.loads((root / "stage/receipt.json").read_text())["returncode"], 3)

    def test_memory_preflight_never_starts_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.call(
                root,
                "raise AssertionError('must never run')",
                minimum_gib=16,
                psutil=observer(1024**3),
            )
            self.assertEqual(result["status"], "not_started")
            self.assertNotIn("pid", result)
            self.assertFalse((root / "stage/stdout.log").exists())

    def test_timeout_stops_child_and_preserves_partial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.call(
                root, "import time; print('partial', flush=True); time.sleep(30)", timeout=0.05
            )
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["reason"], "stage deadline")
            self.assertIn("partial", (root / "stage/stdout.log").read_text())

    def test_sustained_low_ram_stops_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.call(Path(tmp), "import time; time.sleep(30)", psutil=observer(1024**3))
            self.assertEqual(result["status"], "stopped")
            self.assertIn("below 2 GiB", result["reason"])

    def test_native_watchdog_stop_is_not_silently_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.call(
                Path(tmp),
                "import sys; print('MLX resource watchdog: stub pressure', "
                "file=sys.stderr); sys.exit(75)",
            )
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["reason"], "native worker resource watchdog stopped")

    def test_expected_text_uses_final_typed_speech_only_and_checks_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "timeline.json").write_text(
                json.dumps(
                    {
                        "segments": [
                            {"type": "SPEECH", "text": "One ordinary sound."},
                            {"type": "SILENCE", "duration_ms": 3000},
                            {"type": "BREATH", "duration_ms": 3000},
                            {"type": "SPEECH", "text": "Return when ready."},
                        ]
                    }
                )
            )
            expected = runner.read_expected(root)
            self.assertEqual(expected, ["One ordinary sound.", "Return when ready."])
            self.assertEqual(
                (root / "expected-spoken.txt").read_text(),
                "One ordinary sound.\n\nReturn when ready.\n",
            )
            with self.assertRaises(ValueError):
                runner.read_expected(root, ["Changed words."])

    def test_existing_stage_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.call(root, "print('first')")
            with self.assertRaises(FileExistsError):
                self.call(root, "print('second')")
            self.assertIn("first", (root / "stage/stdout.log").read_text())

    def test_only_new_dedicated_date_root_is_allowed_inside_original_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            assets, source, publication = base / "original", base / "source", base / "publication"
            fresh = assets / "evaluations/benchmarks/2026-10-29"
            fresh.mkdir(parents=True)
            source.mkdir()
            publication.mkdir()
            with patch.dict(
                runner.PLAN,
                {
                    "assets": str(assets),
                    "publication": str(publication),
                    "fresh_benchmarks_root": str(fresh),
                },
            ):
                self.assertEqual(
                    runner.validated_output(fresh / "fresh-id", source), fresh / "fresh-id"
                )
                self.assertEqual(
                    runner.validated_output(base / "outside-original", source),
                    base / "outside-original",
                )
                for forbidden in (
                    fresh,
                    assets / "models/new",
                    assets / "recovery/new",
                    assets / "evaluations/benchmarks/2026-10-28/new",
                    source / "new",
                    publication / "new",
                ):
                    with self.assertRaises(ValueError):
                        runner.validated_output(forbidden, source)
                try:
                    (fresh / "escaped").symlink_to(assets / "models", target_is_directory=True)
                except OSError:
                    return  # Windows may require developer mode for symlink creation.
                with self.assertRaises(ValueError):
                    runner.validated_output(fresh / "escaped/new", source)

    @unittest.skipUnless(shutil.which("git"), "Git is required for the real ignore-consumer check")
    def test_new_campaign_ignores_payloads_before_any_evidence_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            campaign = root / "evaluations/benchmarks/2026-10-29/overnight-e2e-fresh"
            runner.create_campaign_directory(campaign)
            paths = [
                campaign / "narration.wav",
                campaign / "source-snapshot/src/example.py",
                campaign / "verify-final.json",
            ]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Stub generated payload")
            relative = [p.relative_to(root).as_posix() for p in paths]
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", *relative],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(set(result.stdout.splitlines()), set(relative))


if __name__ == "__main__":
    unittest.main()
