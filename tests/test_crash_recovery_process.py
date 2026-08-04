from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.pipeline import LocalWorker, RunStatus, RunStore

CHILD_WORKER = r"""
import sys
import time
from pathlib import Path

from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.pipeline import LocalWorker, RunStore


class StopOnSecondSegment:
    def __init__(self, marker):
        self._fixture = FixtureSpeechSynthesizer()
        self._marker = marker
        self.metadata = self._fixture.metadata
        self.cache_identity = self._fixture.cache_identity
        self.sample_rate = self._fixture.sample_rate

    def synthesize(self, segment):
        if segment.id == "speech-0002":
            self._marker.write_text("second segment entered", encoding="utf-8")
            time.sleep(120)
        return self._fixture.synthesize(segment)

    def close(self):
        pass


store = RunStore(Path(sys.argv[1]))
LocalWorker(
    store,
    synthesizer=StopOnSecondSegment(Path(sys.argv[2])),
).process(sys.argv[3])
"""


@pytest.mark.skipif(sys.platform == "win32", reason="process.kill is SIGKILL only on POSIX")
def test_sigkill_releases_run_lock_and_preserves_completed_checkpoint(tmp_path: Path) -> None:
    """Exercise recovery through a real process death, not an in-process exception."""

    store = RunStore(tmp_path / "runs")
    queued = store.create("Let your shoulders soften.")
    marker = tmp_path / "entered-second-segment"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            CHILD_WORKER,
            str(store.root),
            str(marker),
            str(queued.run_id),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.is_file() and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("child worker did not reach its second speech segment")
            time.sleep(0.02)
        assert process.poll() is None, process.stderr.read() if process.stderr is not None else ""

        process.kill()
        assert process.wait(timeout=5) < 0

        interrupted = store.reconcile_stale_run(
            queued.run_id,
            now=datetime.now(UTC) + timedelta(seconds=30),
        )
        assert interrupted.status is RunStatus.INTERRUPTED
        assert interrupted.execution is not None
        assert interrupted.execution.interruption_kind == "lease_expired"

        completed = LocalWorker(
            store,
            synthesizer=FixtureSpeechSynthesizer(),
        ).resume(queued.run_id)
        assert completed.status is RunStatus.COMPLETED
        assert completed.recovery is not None
        assert completed.recovery.checkpoint_reuses == 1
        assert completed.recovery.speech_segments_completed == 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
