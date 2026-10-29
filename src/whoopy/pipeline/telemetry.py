"""Truthful, best-effort resource measurements for one production attempt."""

from __future__ import annotations

import threading
from pathlib import Path

import psutil

from whoopy.hardware import inspect_hardware
from whoopy.pipeline.generation import HardwareTelemetry, TelemetryMeasurement


def unavailable(unit: str, reason: str) -> TelemetryMeasurement:
    """Build a bounded explicit-unavailability measurement."""

    return TelemetryMeasurement.model_validate({"unit": unit, "unavailable_reason": reason[:500]})


def measured(unit: str, value: float | int) -> TelemetryMeasurement:
    """Build a non-negative measured value."""

    return TelemetryMeasurement.model_validate({"unit": unit, "value": value})


def hardware_snapshot(path: Path) -> HardwareTelemetry:
    """Inspect hardware without allowing telemetry failure to fail production."""

    try:
        return HardwareTelemetry(snapshot=inspect_hardware(path))
    except Exception as error:  # telemetry must never replace the production error
        return HardwareTelemetry(unavailable_reason=f"{type(error).__name__}: {error}"[:500])


class PeakProcessTreeMemory:
    """Sample this process and its children while a model attempt is active."""

    def __init__(self, *, interval_seconds: float = 0.02) -> None:
        self.interval_seconds = interval_seconds
        self.peak_bytes: int | None = None
        self.live_bytes_after: int | None = None
        self.unavailable_reason: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _rss_bytes() -> int:
        process = psutil.Process()
        total = 0
        for current in (process, *process.children(recursive=True)):
            try:
                total += current.memory_info().rss
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        return total

    def _sample(self) -> None:
        try:
            while not self._stop.wait(self.interval_seconds):
                current = self._rss_bytes()
                self.peak_bytes = max(self.peak_bytes or 0, current)
        except (psutil.Error, OSError) as error:
            self.unavailable_reason = f"{type(error).__name__}: {error}"[:500]

    def start(self) -> None:
        try:
            self.peak_bytes = self._rss_bytes()
        except (psutil.Error, OSError) as error:
            self.unavailable_reason = f"{type(error).__name__}: {error}"[:500]
            return
        self._thread = threading.Thread(
            target=self._sample,
            name="whoopy-run-memory-telemetry",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self.unavailable_reason is not None:
            return
        try:
            self.live_bytes_after = self._rss_bytes()
            self.peak_bytes = max(self.peak_bytes or 0, self.live_bytes_after)
        except (psutil.Error, OSError) as error:
            self.unavailable_reason = f"{type(error).__name__}: {error}"[:500]
