from __future__ import annotations

import json
import signal
import struct
import sys
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest

from scripts import benchmark_voice_candidates, dots_tts_full_meditation


def _evidence(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("changed_input", [None, "base_seed", "script_sha256", "reference_sha256"])
def test_dots_rejects_previous_evidence_before_loading_or_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_input: str | None
) -> None:
    folder = tmp_path / "dots"
    (folder / "speech").mkdir(parents=True)
    (folder / "speech" / "speech-0001.wav").write_bytes(b"native old audio")
    (folder / "speech" / "speech-0001.json").write_text('{"seed": 42}')
    (folder / "voxcpm2-deep-male.dtprofile").mkdir()
    (folder / "voxcpm2-deep-male.dtprofile" / "profile.json").write_text('{"old": true}')
    plan: dict[str, object] = {
        "base_seed": 42,
        "script_sha256": "old-script",
        "reference_sha256": "old-reference",
    }
    (folder / "plan.json").write_text(json.dumps(plan))
    before = _evidence(folder)
    if changed_input is not None:
        plan[changed_input] = 43 if changed_input == "base_seed" else "different-hash"
    monkeypatch.setitem(sys.modules, "mlx", None)

    with pytest.raises(SystemExit, match="use a new output directory"):
        dots_tts_full_meditation.run_heavyweight(
            events=[],
            plan=plan,
            model_path=tmp_path / "model",
            reference=tmp_path / "reference.wav",
            reference_text="New reference",
            output_dir=folder,
            enroll_only=False,
        )

    assert _evidence(folder) == before


def test_dots_fresh_folder_enrolls_and_records_its_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"synthetic reference fixture")
    folder = tmp_path / "fresh"
    calls: list[tuple[str, str, float]] = []

    class Profile:
        schema_version = 1
        compat_hash = "fixture-model"
        prompt_patch_count = 2
        speaker_scale = 1.5
        sample_rate = 48_000

        def save(self, path: Path) -> None:
            path.mkdir()
            (path / "profile.json").write_text('{"fixture": true}')

    class Model:
        def enroll(self, path: str, text: str, *, speaker_scale: float) -> Profile:
            calls.append((path, text, speaker_scale))
            return Profile()

    mx = SimpleNamespace(bfloat16="fixture-dtype", get_peak_memory=lambda: 1024)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mx))
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setitem(
        sys.modules,
        "dots_tts_mlx.loader",
        SimpleNamespace(from_pretrained=lambda path, dtype: SimpleNamespace(model=Model())),
    )
    plan: dict[str, object] = {"speaker_scale": 1.5, "base_seed": 42}

    dots_tts_full_meditation.run_heavyweight(
        events=[],
        plan=plan,
        model_path=tmp_path / "model",
        reference=reference,
        reference_text="Reference words",
        output_dir=folder,
        enroll_only=True,
    )

    receipt = json.loads((folder / "enrollment.json").read_text())
    assert calls == [(str(reference), "Reference words", 1.5)]
    assert receipt["reference_sha256"] == dots_tts_full_meditation.sha256(reference)
    assert receipt["enrolled_during_this_run"] is True
    assert json.loads((folder / "plan.json").read_text()) == plan


@pytest.mark.parametrize("saved_file", ["report.json", "segment-00.wav", "profile/data.bin"])
def test_voice_candidate_refuses_partial_and_complete_existing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved_file: str
) -> None:
    output = tmp_path / "voices"
    folder = output / "kokoro-am-michael"
    path = folder / saved_file
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original evidence")
    before = _evidence(folder)
    monkeypatch.setattr(benchmark_voice_candidates, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark_voice_candidates, "OUT", output)
    monkeypatch.setitem(sys.modules, "numpy", None)

    with pytest.raises(SystemExit, match="use a new output directory"):
        benchmark_voice_candidates.run("kokoro-am-michael", 43)

    assert _evidence(folder) == before


def test_voice_candidate_cli_passes_fresh_retry_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "retry"
    calls: list[tuple[str, int, Path | None]] = []

    def record_run(name: str, seed: int, *, output_dir: Path | None = None) -> None:
        calls.append((name, seed, output_dir))

    monkeypatch.setattr(benchmark_voice_candidates, "run", record_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark", "run", "kokoro-am-michael", "--seed", "43", "--output-dir", str(folder)],
    )

    benchmark_voice_candidates.main()

    assert calls == [("kokoro-am-michael", 43, folder)]
    assert not folder.exists()


def test_voice_candidate_relative_output_finishes_with_resolvable_artifact_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Samples:
        ndim = 1

        def __init__(self, values: list[int]) -> None:
            self.values = values
            self.size = len(values)
            self.shape = (len(values),)

    def concatenate(chunks: list[Samples], *, axis: int) -> Samples:
        assert axis == 0
        return Samples([value for chunk in chunks for value in chunk.values])

    def write_fixture_wave(path: Path, samples: Samples, rate: int, *, subtype: str) -> None:
        assert subtype == "FLOAT"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(struct.pack(f"<{samples.size}h", *samples.values))

    np = SimpleNamespace(
        random=SimpleNamespace(seed=lambda _: None),
        float32="fixture-dtype",
        asarray=lambda samples, dtype: samples,
        concatenate=concatenate,
        isfinite=lambda samples: SimpleNamespace(all=lambda: True),
    )
    monkeypatch.setitem(sys.modules, "numpy", np)
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(write=write_fixture_wave))
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(
            virtual_memory=lambda: SimpleNamespace(available=32 * 1024**3),
            swap_memory=lambda: SimpleNamespace(used=0),
        ),
    )

    module = benchmark_voice_candidates
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "voice-script.txt"
    script.write_text("First paragraph.\n\nSecond paragraph.")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "SCRIPT", script)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(signal, "alarm", lambda *args: None)
    monkeypatch.setattr(
        module,
        "ResourceWatchdog",
        lambda *args: SimpleNamespace(start=lambda: None, finish=lambda: {}, reason=None),
    )
    original_open = Path.open

    def isolated_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[Any]:
        if path == Path("/tmp/whoopy-benchmark-gpu.lock"):
            path = tmp_path / "gpu.lock"
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", isolated_open)
    mx = SimpleNamespace(
        random=SimpleNamespace(seed=lambda _: None),
        set_memory_limit=lambda _: None,
        reset_peak_memory=lambda: None,
        get_peak_memory=lambda: 0,
        eval=lambda _: None,
    )
    calls: list[str] = []

    def generate(*, text: str, **kwargs: object) -> Iterator[SimpleNamespace]:
        calls.append(text)
        yield SimpleNamespace(audio=Samples([100, -100, 200]), sample_rate=24_000)

    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mx))
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setitem(
        sys.modules,
        "mlx_audio.tts.utils",
        SimpleNamespace(load_model=lambda _: SimpleNamespace(generate=generate)),
    )

    module.run("kokoro-am-michael", 43, output_dir=Path("evaluations/benchmarks/retry"))

    report = json.loads((tmp_path / "evaluations/benchmarks/retry/report.json").read_text())
    assert report["status"] == "complete"
    assert calls == ["First paragraph.", "Second paragraph."]
    assert report["raw_path"] == "evaluations/benchmarks/retry/raw.wav"
    for segment in report["segments"]:
        path = tmp_path / segment["path"]
        assert path.is_file()
        assert module.digest(path) == segment["sha256"]
    assert module.digest(tmp_path / report["raw_path"]) == report["raw_sha256"]


def test_voice_candidate_rejects_destination_outside_root_before_claiming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(benchmark_voice_candidates, "ROOT", root)
    monkeypatch.setitem(sys.modules, "numpy", None)
    output = tmp_path / "outside"

    with pytest.raises(SystemExit, match="inside the benchmark project root"):
        benchmark_voice_candidates.run("kokoro-am-michael", 43, output_dir=output)

    assert not output.exists()
