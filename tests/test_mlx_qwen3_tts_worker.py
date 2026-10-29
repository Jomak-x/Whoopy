from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import runpy
import struct
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest

WORKER = Path(__file__).resolve().parents[1] / "scripts/mlx_qwen3_tts_worker.py"


@pytest.fixture
def worker() -> dict[str, Any]:
    return runpy.run_path(str(WORKER))


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "task": "custom_voice",
        "text": "Settle here.",
        "language": "English",
        "seed": 42,
        "speaker": "Ryan",
        "instruction": "Calm.",
        "segment_id": "segment-01",
        "native_artifact_key": "a" * 64,
    }


class Audio:
    def __init__(self, samples: Iterable[float], dtype: str = "float32", ndim: int = 1) -> None:
        self.samples = list(samples)
        self.ndim = ndim
        self.shape = (len(self.samples),)
        self.size = len(self.samples)
        self.dtype = SimpleNamespace(kind="f" if dtype == "float32" else "i")

    def astype(self, dtype: str, copy: bool = False) -> Any:
        if dtype == "<i2":
            return SimpleNamespace(
                tobytes=lambda: struct.pack("<" + "h" * self.size, *self.samples)
            )
        return self

    def __mul__(self, factor: float) -> Audio:
        return Audio([sample * factor for sample in self.samples])


class FakeNP:
    float32 = "float32"

    @staticmethod
    def asarray(audio: Audio) -> Audio:
        return audio

    @staticmethod
    def isfinite(audio: Audio) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: all(math.isfinite(x) for x in audio.samples))

    @staticmethod
    def concatenate(chunks: Iterable[Audio]) -> Audio:
        return Audio([x for chunk in chunks for x in chunk.samples])

    @staticmethod
    def clip(audio: Audio, lower: float, upper: float) -> Audio:
        return Audio([max(lower, min(upper, x)) for x in audio.samples])

    @staticmethod
    def rint(audio: Audio) -> Audio:
        return Audio([round(x) for x in audio.samples])

    @staticmethod
    def save(handle: BinaryIO, audio: Audio, *, allow_pickle: bool) -> None:
        assert allow_pickle is False
        handle.write(struct.pack("<" + "f" * audio.size, *audio.samples))


class FakeSF:
    def __init__(self) -> None:
        self.calls: list[tuple[list[float], int, dict[str, Any]]] = []

    def write(self, handle: BinaryIO, audio: Audio, rate: int, **kwargs: Any) -> None:
        self.calls.append((audio.samples, rate, kwargs))
        # A tiny float-preserving stand-in for libsndfile, never used by production.
        handle.write(struct.pack("<" + "f" * audio.size, *audio.samples))


class FakeMLX:
    def __init__(self) -> None:
        self.seeds: list[int] = []
        self.set_memory_limit: Callable[[int], None] = lambda _limit: None
        self.random = SimpleNamespace(seed=self.seeds.append)

    def eval(self, _audio: Audio) -> None:
        pass


class FakeModel:
    def __init__(self, audio: Audio | None = None, rate: float = 24000, eos: bool = True) -> None:
        self.audio = Audio([1.25, 0.125, -1.25]) if audio is None else audio
        self.rate, self.eos = rate, eos
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Iterator[SimpleNamespace]:
        self.calls.append(kwargs)
        # Match the native generator's exposed locals without importing MLX.
        is_eos = SimpleNamespace(item=lambda: self.eos)
        generated_codes = [1, 2, 3]
        max_tokens = kwargs["max_tokens"]
        assert is_eos is not None and generated_codes and max_tokens
        yield SimpleNamespace(audio=self.audio, sample_rate=self.rate)


def synthesize(
    worker: dict[str, Any], payload: dict[str, Any], tmp_path: Path, model: Any = None
) -> tuple[dict[str, Any], Any, FakeMLX, FakeSF]:
    mx, sf = FakeMLX(), FakeSF()
    model = model or FakeModel()
    response = worker["_synthesize"](
        payload,
        task=payload["task"],
        model=model,
        model_path=Path("/local/pinned/checkpoint"),
        raw_dir=tmp_path,
        mx=mx,
        np=FakeNP,
        sf=sf,
    )
    return response, model, mx, sf


def test_import_is_light_and_native_stdout_is_isolated() -> None:
    program = f"""
import runpy, sys, os
worker = runpy.run_path({str(WORKER)!r})
assert not any(x in sys.modules for x in ('mlx', 'mlx_audio', 'numpy', 'soundfile', 'psutil'))
worker['_isolate_protocol_output']()
os.write(1, b'native import warning\\n')
worker['_reply']({{'status': 'ready'}})
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "ready"}
    assert "native import warning" in result.stderr


def test_offline_settings_override_inherited_online_environment(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    worker["_offline_environment"]()
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_native_float_evidence_is_preserved_before_pcm_conversion(
    worker: dict[str, Any], payload: dict[str, Any], tmp_path: Path
) -> None:
    response, model, mx, sf = synthesize(worker, payload, tmp_path)
    assert response["status"] == "ok"
    assert mx.seeds == [42]
    assert model.calls == [
        {
            "text": "Settle here.",
            "lang_code": "English",
            "instruct": "Calm.",
            "voice": "Ryan",
            "max_tokens": 192,
            "stream": False,
            "verbose": False,
        }
    ]
    evidence = response["raw_evidence"]
    native = Path(evidence["wav_path"])
    samples = struct.unpack("<fff", native.read_bytes())
    assert list(samples) == model.audio.samples
    assert sf.calls == [(model.audio.samples, 24000, {"format": "WAV", "subtype": "FLOAT"})]
    assert evidence["frames"] == 3 and evidence["eos_observed"] is True
    assert evidence["sha256"] == hashlib.sha256(native.read_bytes()).hexdigest()
    assert evidence["inference_seconds"] >= 0
    sidecar = Path(evidence["sidecar_path"])
    metadata = json.loads(sidecar.read_text())
    assert metadata["seed"] == 42 and metadata["experimental"] is True
    assert metadata["text"] == payload["text"]
    assert (
        metadata["pcm_s16le_sha256"]
        == hashlib.sha256(base64.b64decode(response["pcm_s16le"])).hexdigest()
    )
    assert evidence["sidecar_sha256"] == hashlib.sha256(sidecar.read_bytes()).hexdigest()
    assert metadata["stop_evidence"]["segments"][0]["generated_codec_steps"] == 3
    assert metadata["mlx_audio_revision"] == "8c244a1faff44cfa69e3151c195f52a4c1c90be5"
    assert native.stat().st_mode & 0o222 == 0
    assert sidecar.stat().st_mode & 0o222 == 0
    assert struct.unpack("<hhh", base64.b64decode(response["pcm_s16le"])) == (32767, 4096, -32767)
    second, _, _, _ = synthesize(worker, payload, tmp_path)
    assert second["raw_evidence"]["wav_path"] != str(native)
    assert evidence["sha256"] == hashlib.sha256(native.read_bytes()).hexdigest()


def test_voice_design_has_no_preset_voice_argument(
    worker: dict[str, Any], payload: dict[str, Any], tmp_path: Path
) -> None:
    payload.update(task="voice_design", speaker=None, instruction="A calm male voice.")
    _, model, _, _ = synthesize(worker, payload, tmp_path)
    assert "voice" not in model.calls[0]
    assert model.calls[0]["instruct"] == "A calm male voice."


@pytest.mark.parametrize(
    "field,value",
    [
        ("task", "voice_clone"),
        ("text", " "),
        ("text", None),
        ("language", "French"),
        ("seed", True),
        ("seed", -1),
        ("seed", 2**32),
        ("speaker", "Vivian"),
        ("instruction", None),
        ("native_artifact_key", "../escape"),
        ("native_artifact_key", "/absolute"),
        ("segment_id", "../escape"),
    ],
)
def test_rejects_invalid_request_before_generation(
    worker: dict[str, Any], payload: dict[str, Any], field: str, value: object
) -> None:
    payload[field] = value
    with pytest.raises(ValueError):
        worker["_request_arguments"](payload, "custom_voice")


@pytest.mark.parametrize("speaker", ["Ryan", "Aiden", "Serena"])
def test_reviewed_custom_speakers(
    worker: dict[str, Any], payload: dict[str, Any], speaker: str
) -> None:
    payload["speaker"] = speaker
    assert worker["_request_arguments"](payload, "custom_voice")["voice"] == speaker


def test_voice_design_requires_instruction_and_rejects_preset(
    worker: dict[str, Any], payload: dict[str, Any]
) -> None:
    payload.update(task="voice_design", instruction="", speaker=None)
    with pytest.raises(ValueError, match="instruction"):
        worker["_request_arguments"](payload, "voice_design")
    payload.update(instruction="calm", speaker="Ryan")
    with pytest.raises(ValueError, match="speaker"):
        worker["_request_arguments"](payload, "voice_design")


@pytest.mark.parametrize(
    "audio,rate",
    [
        (Audio([]), 24000),
        (Audio([0.1], ndim=2), 24000),
        (Audio([float("nan")]), 24000),
        (Audio([float("inf")]), 24000),
        (Audio([1], dtype="int16"), 24000),
        (Audio([0.1]), 22050),
        (Audio([0.1]), float("nan")),
        (Audio([0.1]), float("inf")),
        (Audio([0.1]), 24000.5),
    ],
)
def test_rejects_non_native_or_invalid_waveforms(
    worker: dict[str, Any], payload: dict[str, Any], tmp_path: Path, audio: Audio, rate: float
) -> None:
    model = FakeModel(audio)
    model.rate = rate
    sf = FakeSF()

    def rejected_attempt() -> dict[str, Any]:
        with pytest.raises(worker["NativeOutputError"], match="native output") as error:
            worker["_synthesize"](
                payload,
                task=payload["task"],
                model=model,
                model_path=tmp_path / "checkpoint",
                raw_dir=tmp_path,
                mx=FakeMLX(),
                np=FakeNP,
                sf=sf,
            )
        evidence: dict[str, Any] = error.value.evidence
        return evidence

    evidence = rejected_attempt()
    assert evidence["status"] == "invalid"
    assert evidence["playable"] is False
    assert evidence["artifact_format"] == "numpy_npy"
    assert "wav_path" not in evidence
    assert "pcm_s16le_sha256" not in evidence
    assert sf.calls == []
    returned = evidence["returned_outputs"][0]
    assert returned["sample_rate_returned"] == repr(rate)
    raw = Path(returned["array_path"])
    assert raw.suffix == ".npy"
    expected = struct.pack("<" + "f" * audio.size, *audio.samples)
    assert raw.read_bytes() == expected
    assert raw.stat().st_mode & 0o222 == 0
    assert returned["sha256"] == hashlib.sha256(expected).hexdigest()
    sidecar = Path(evidence["sidecar_path"])
    metadata = json.loads(sidecar.read_text())
    assert metadata["rejection_reason"] == (
        "native output must be nonempty finite mono float audio at 24000 Hz"
    )
    assert metadata["returned_outputs"] == evidence["returned_outputs"]
    assert metadata["request"] == payload
    assert metadata["stop_evidence"][0]["generated_codec_steps"] == 3
    assert sidecar.stat().st_mode & 0o222 == 0
    original_sidecar = sidecar.read_bytes()
    assert evidence["sidecar_sha256"] == hashlib.sha256(original_sidecar).hexdigest()
    second = rejected_attempt()
    assert second["attempt_id"] != evidence["attempt_id"]
    assert second["returned_outputs"][0]["array_path"] != str(raw)
    assert raw.read_bytes() == expected
    assert sidecar.read_bytes() == original_sidecar


def test_later_invalid_yield_retains_all_returned_audio_without_pcm(
    worker: dict[str, Any], payload: dict[str, Any], tmp_path: Path
) -> None:
    first, invalid = Audio([0.125, -0.25]), Audio([float("nan"), float("inf")])
    model = SimpleNamespace(
        generate=lambda **kwargs: iter(
            SimpleNamespace(audio=audio, sample_rate=24000) for audio in (first, invalid)
        )
    )
    with pytest.raises(worker["NativeOutputError"], match="native output") as error:
        synthesize(worker, payload, tmp_path, model)
    evidence = error.value.evidence
    assert evidence["status"] == "invalid"
    assert "pcm_s16le_sha256" not in evidence
    returned = evidence["returned_outputs"]
    assert len(returned) == 2
    for item, original in zip(returned, (first, invalid), strict=True):
        assert Path(item["array_path"]).read_bytes() == struct.pack("<ff", *original.samples)
    assert list(tmp_path.rglob("*.wav")) == []


def test_missing_eos_preserves_native_artifact_and_fails_closed(
    worker: dict[str, Any], payload: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(worker["NativeOutputError"], match="EOS") as error:
        synthesize(worker, payload, tmp_path, FakeModel(eos=False))
    evidence = error.value.evidence
    assert evidence["eos_observed"] is False
    assert Path(evidence["wav_path"]).is_file()
    assert Path(evidence["sidecar_path"]).is_file()


def test_memory_admission_and_cap(worker: dict[str, Any]) -> None:
    def memory(available: int) -> SimpleNamespace:
        return SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=available))

    gib = 1024**3
    with pytest.raises(RuntimeError, match="16 GiB"):
        worker["_admission_limit"](memory(16 * gib - 1))
    assert worker["_admission_limit"](memory(16 * gib)) == 10 * gib
    assert worker["_admission_limit"](memory(40 * gib)) == 24 * gib


def test_watchdog_requires_sustained_pressure_and_resets(worker: dict[str, Any]) -> None:
    gib = 1024**3
    psutil = SimpleNamespace(swap_memory=lambda: SimpleNamespace(used=gib))
    watchdog = worker["ResourceWatchdog"](psutil)
    assert watchdog.observe(gib, gib, 0) is None
    assert watchdog.observe(gib, gib, 1.9) is None
    assert watchdog.observe(3 * gib, gib, 2) is None
    assert watchdog.observe(gib, gib, 3) is None
    assert watchdog.observe(gib, gib, 5) == "available RAM below 2 GiB sustained 2 seconds"
    swap_reason = watchdog.observe(3 * gib, 5 * gib + 1, 6)
    assert swap_reason is not None and "swap" in swap_reason


def test_gpu_lock_is_held_until_descriptor_closes(worker: dict[str, Any], tmp_path: Path) -> None:
    if sys.platform == "win32" or os.name != "posix":
        pytest.skip("flock is only available on POSIX")
    else:
        lock_path = tmp_path / "private-gpu.lock"
        real_open = os.open
        lock = worker["_gpu_lock"]
        lock.__globals__["os"] = SimpleNamespace(
            name="posix",
            open=lambda _path, flags, mode: real_open(lock_path, flags, mode),
            O_CREAT=os.O_CREAT,
            O_RDWR=os.O_RDWR,
            O_NOFOLLOW=os.O_NOFOLLOW,
            close=os.close,
        )
        fd = lock()
        program = (
            "import fcntl,sys; f=open(sys.argv[1], 'rb'); "
            "fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)"
        )
        try:
            blocked = subprocess.run(
                [sys.executable, "-c", program, str(lock_path)], capture_output=True, timeout=10
            )
            assert blocked.returncode != 0
        finally:
            os.close(fd)
        acquired = subprocess.run(
            [sys.executable, "-c", program, str(lock_path)], capture_output=True, timeout=10
        )
        assert acquired.returncode == 0


def test_json_loop_reports_bad_requests_and_closes_without_runtime_inference(
    worker: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    from types import ModuleType

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    replies: list[dict[str, Any]] = []
    mx = FakeMLX()
    limits: list[int] = []
    mx.set_memory_limit = limits.append
    psutil = SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(available=32 * 1024**3),
        swap_memory=lambda: SimpleNamespace(used=0),
    )
    model = SimpleNamespace(
        config=SimpleNamespace(tts_model_type="custom_voice"), sample_rate=24000
    )
    for name, value in {
        "psutil": psutil,
        "mlx": ModuleType("mlx"),
        "mlx.core": mx,
        "numpy": FakeNP,
        "soundfile": FakeSF(),
        "mlx_audio": ModuleType("mlx_audio"),
        "mlx_audio.tts": ModuleType("mlx_audio.tts"),
        "mlx_audio.tts.utils": SimpleNamespace(load_model=lambda path: model),
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    globals_ = worker["main"].__globals__
    monkeypatch.setitem(
        globals_,
        "_arguments",
        lambda: SimpleNamespace(model=model_dir, task="custom_voice", raw_output_dir=tmp_path),
    )
    monkeypatch.setitem(globals_, "_isolate_protocol_output", lambda: None)
    monkeypatch.setitem(globals_, "_require_supported_host", lambda _platform, _arch: None)
    monkeypatch.setitem(globals_, "_gpu_lock", lambda: None)
    monkeypatch.setitem(globals_, "_reply", replies.append)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            'not json\n[]\n{"request_id":"bad","task":"voice_clone"}\n'
            '{"request_id":"done","action":"close"}\n'
        ),
    )
    assert worker["main"]() == 0
    assert replies[0]["status"] == "ready"
    assert replies[0]["device"] == "mlx-metal"
    assert replies[0]["sample_rate"] == 24000
    assert all(item["status"] == "error" for item in replies[1:4])
    assert replies[3]["request_id"] == "bad"
    assert replies[4] == {"status": "closing", "request_id": "done"}
    assert limits == [24 * 1024**3]


def test_generation_without_chunks_fails_closed(
    worker: dict[str, Any], payload: dict[str, Any], tmp_path: Path
) -> None:
    model = SimpleNamespace(generate=lambda **kwargs: iter(()))
    with pytest.raises(RuntimeError, match="no audio"):
        synthesize(worker, payload, tmp_path, model)
    assert list(tmp_path.iterdir()) == []


def test_eos_unavailable_fails_closed(worker: dict[str, Any]) -> None:
    iterator = iter([1])
    assert worker["_generator_stop_evidence"](iterator)["codec_eos_observed"] is False


@pytest.mark.parametrize(
    "steps,budget,eos,accepted",
    [
        (191, 192, True, True),
        (192, 192, False, False),
        (192, 192, True, False),
        (193, 192, True, False),
        (0, 192, True, False),
    ],
)
def test_eos_budget_uses_code_count_after_eos_before_append(
    worker: dict[str, Any], steps: int, budget: int, eos: bool, accepted: bool
) -> None:
    frame = SimpleNamespace(
        f_locals={
            "is_eos": SimpleNamespace(item=lambda: eos),
            "generated_codes": [0] * steps,
            "max_tokens": budget,
        }
    )
    result = worker["_generator_stop_evidence"](SimpleNamespace(gi_frame=frame))
    assert result["codec_eos_observed"] is eos
    assert result["completion_valid"] is accepted


@pytest.mark.parametrize("missing", ["is_eos", "generated_codes", "max_tokens"])
def test_missing_native_completion_local_fails_closed_without_throwing(
    worker: dict[str, Any], missing: str
) -> None:
    values = {
        "is_eos": SimpleNamespace(item=lambda: True),
        "generated_codes": [0, 1],
        "max_tokens": 192,
    }
    del values[missing]
    result = worker["_generator_stop_evidence"](
        SimpleNamespace(gi_frame=SimpleNamespace(f_locals=values))
    )
    assert result["completion_valid"] is False


def test_partial_old_attempt_is_retained_and_never_reused(
    worker: dict[str, Any], payload: dict[str, Any], tmp_path: Path
) -> None:
    partial = tmp_path / (payload["native_artifact_key"] + "-crashed")
    partial.mkdir()
    (partial / "native.wav").write_bytes(b"incomplete historical evidence")
    result, _, _, _ = synthesize(worker, payload, tmp_path)
    assert (partial / "native.wav").read_bytes() == b"incomplete historical evidence"
    assert not (partial / "metadata.json").exists()
    assert Path(result["raw_evidence"]["sidecar_path"]).parent != partial


def test_gpu_lock_rejects_unsupported_platform_without_importing_fcntl(
    worker: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = worker["_gpu_lock"]
    monkeypatch.setitem(lock.__globals__, "os", SimpleNamespace(name="nt"))
    with pytest.raises(RuntimeError, match="requires POSIX flock"):
        lock()


@pytest.mark.parametrize(
    "platform_name,architecture",
    [
        ("win32", "AMD64"),
        ("win32", "arm64"),
        ("linux", "x86_64"),
        ("linux", "arm64"),
        ("darwin", "x86_64"),
    ],
)
def test_host_guard_rejects_unsupported_platforms_before_runtime_imports(
    worker: dict[str, Any], platform_name: str, architecture: str
) -> None:
    with pytest.raises(RuntimeError, match="requires macOS Apple Silicon") as error:
        worker["_require_supported_host"](platform_name, architecture)
    assert f"{platform_name}/{architecture}" in str(error.value)


def test_host_guard_accepts_darwin_arm64(worker: dict[str, Any]) -> None:
    assert worker["_require_supported_host"]("darwin", "arm64") is None
