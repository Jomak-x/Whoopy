#!/usr/bin/env python3
"""Isolated, fixed-text local voice audition; never modifies managed model packs.

Run download with the isolated MLX Python, then run one candidate per process.
Every inference holds /tmp/whoopy-benchmark-gpu.lock. Raw FLOAT WAVs retain
native samples. Paragraph boundaries are shared across candidates; assembly
only concatenates returned samples, adding no silence or processing.
"""

from __future__ import annotations

import _thread
import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import math
import resource
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "models/experimental/voice-benchmark-2026-10-28"
OUT = ROOT / "evaluations/benchmarks/2026-10-28/voices"
SCRIPT = OUT.parent / "voice-script.txt"
MLX_REVISION = "8c244a1faff44cfa69e3151c195f52a4c1c90be5"
REPOS: dict[str, tuple[str, str]] = {
    "kokoro": ("mlx-community/Kokoro-82M-bf16", "a71e4d38b236d968966a2002c4c895dbd12b1c3c"),
    "voxcpm2": ("mlx-community/VoxCPM2-8bit", "d52725898a0675703f7f9ddc5a4d1a3cdbb99032"),
    "chatterbox-v3": (
        "mlx-community/chatterbox-multilingual-v3",
        "03565773edd72e949572557597af8063bb49a18a",
    ),
    "s3tokenizer": ("mlx-community/S3TokenizerV2", "e0c9886f0e1c35ae85b1f27277416fb19fc72bec"),
    "moss-local": (
        "mlx-community/MOSS-TTS-Local-Transformer-v1.5-4bit",
        "613364100996995dd095f7abd04b24372ce11a8e",
    ),
    "dots-soar": ("shraey/dots-tts-mlx", "9cfc15b4863f7d01bad348a28fe8d05c40a0e95e"),
    "mlx-qwen-design": (
        "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
        "f90d617701d9f7f4ca499291e0b57f2b3c2fd2ee",
    ),
    "mlx-qwen-custom": (
        "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
        "41d3337e8b7f2843a75841595fc14e4b9a7a4b96",
    ),
}
CANDIDATES: dict[str, tuple[str, dict[str, Any]]] = {
    "kokoro-am-michael": ("kokoro", {"voice": "am_michael", "lang_code": "a", "speed": 1.0}),
    "kokoro-bm-george-080": ("kokoro", {"voice": "bm_george", "lang_code": "b", "speed": 0.8}),
    "kokoro-af-heart-080": ("kokoro", {"voice": "af_heart", "lang_code": "a", "speed": 0.8}),
    "voxcpm2-designed-male": (
        "voxcpm2",
        {
            "instruct": (
                "An adult man with a deep warm resonant voice, calm clear English, "
                "relaxed natural pacing, gentle meditation guidance."
            ),
            "inference_timesteps": 10,
            "cfg_value": 2.0,
            "max_tokens": 1500,
        },
    ),
    "chatterbox-v3-synthetic-male": (
        "chatterbox-v3",
        {
            "lang_code": "en",
            "exaggeration": 0.1,
            "cfg_weight": 0.5,
            "temperature": 0.8,
            "max_new_tokens": 1500,
            "verbose": False,
        },
    ),
    "moss-local-synthetic-male": ("moss-local", {"max_tokens": 2048}),
    "dots-soar-synthetic-male": (
        "dots-soar",
        {
            "num_steps": 10,
            "guidance_scale": 1.2,
            "speaker_scale": 1.5,
            "language": "EN",
            "max_generate_length": 350,
            "trim_onset": False,
            "streaming_decode": True,
        },
    ),
    "qwen-custom-ryan": (
        "qwen-custom",
        {
            "speaker": "Ryan",
            "instruct": (
                "Speak calmly and warmly, with a deep natural male voice and relaxed clear pacing."
            ),
        },
    ),
    "qwen-designed-male": (
        "qwen-design",
        {
            "instruct": (
                "An adult man with a deep warm resonant baritone voice, clear English, "
                "calm natural delivery, relaxed pacing and gentle meditation guidance. "
                "No whispering."
            )
        },
    ),
}
CANDIDATES["voxcpm2-reference"] = CANDIDATES["voxcpm2-designed-male"]
CANDIDATES["mlx-qwen-designed-male"] = (
    "mlx-qwen-design",
    {**CANDIDATES["qwen-designed-male"][1], "lang_code": "English"},
)
CANDIDATES["mlx-qwen-custom-ryan"] = (
    "mlx-qwen-custom",
    {
        "voice": "Ryan",
        "lang_code": "English",
        "instruct": CANDIDATES["qwen-custom-ryan"][1]["instruct"],
    },
)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def qwen_token_budget(text: str) -> int:
    """Match the current production worker's reviewed text-based codec budget."""
    units = max(1, len(text.split()), math.ceil(len(text.strip()) / 4))
    return min(2048, max(192, 96 + 12 * units))


def segment_timeout(signum: int, frame: Any) -> None:
    del signum, frame
    raise TimeoutError("segment synthesis exceeded the eight-minute safety deadline")


class ResourceWatchdog:
    """Observe total system pressure; allocator limits alone are insufficient."""

    def __init__(self, folder: Path, psutil_module: Any) -> None:
        self.folder = folder
        self.psutil = psutil_module
        self.stop_event = threading.Event()
        self.observations: list[dict[str, Any]] = []
        self.baseline_available = int(self.psutil.virtual_memory().available)
        self.baseline_swap = int(self.psutil.swap_memory().used)
        self.reason: str | None = None
        self.low_memory_since: float | None = None
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "unix_time": time.time(),
            "monotonic_time": time.monotonic(),
            "available_bytes": int(self.psutil.virtual_memory().available),
            "swap_used_bytes": int(self.psutil.swap_memory().used),
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }

    def payload(self) -> dict[str, Any]:
        return {
            "baseline_available_bytes": self.baseline_available,
            "baseline_swap_used_bytes": self.baseline_swap,
            "poll_seconds": 0.5,
            "swap_growth_limit_bytes": 4 * 1024**3,
            "available_floor_bytes": 2 * 1024**3,
            "available_floor_sustain_seconds": 2,
            "reason": self.reason,
            "observations": list(self.observations),
            "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }

    def start(self) -> None:
        self.observations.append(self._snapshot())
        self.thread.start()

    def _poll(self) -> None:
        while not self.stop_event.wait(0.5):
            try:
                observation = self._snapshot()
            except Exception as error:
                self.reason = f"resource observer failed: {type(error).__name__}: {error}"
                save(self.folder / "resource-watchdog.json", self.payload())
                _thread.interrupt_main()
                return
            self.observations.append(observation)
            now = observation["monotonic_time"]
            if observation["available_bytes"] < 2 * 1024**3:
                if self.low_memory_since is None:
                    self.low_memory_since = now
                if now - self.low_memory_since >= 2:
                    self.reason = "available RAM below 2 GiB sustained 2 seconds"
            else:
                self.low_memory_since = None
            if observation["swap_used_bytes"] - self.baseline_swap > 4 * 1024**3:
                self.reason = "system swap grew more than 4 GiB above candidate baseline"
            if self.reason:
                # Save before interrupting: evidence survives even if native code
                # delays Python's KeyboardInterrupt while completing a kernel.
                save(self.folder / "resource-watchdog.json", self.payload())
                _thread.interrupt_main()
                return

    def finish(self) -> dict[str, Any]:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        result = self.payload()
        save(self.folder / "resource-watchdog.json", result)
        return result


def generator_stop_evidence(iterator: Any, family: str) -> dict[str, Any]:
    """Read exposed native generator locals at yield without changing decoding."""
    generator = iterator
    while getattr(generator, "gi_yieldfrom", None) is not None:
        generator = generator.gi_yieldfrom
    frame = getattr(generator, "gi_frame", None)
    values = frame.f_locals if frame is not None else {}
    if family.startswith("mlx-qwen-") and "is_eos" in values:
        return {
            "codec_eos_observed": bool(values["is_eos"].item()),
            "generated_codec_steps": len(values["generated_codes"]),
            "max_tokens": values["max_tokens"],
            "evidence": "native generator is_eos at final nonstreaming yield",
        }
    if family == "voxcpm2" and "stop_flag" in values:
        return {
            "native_stop_flag": int(values["stop_flag"]),
            "generated_patches": len(values["pred_feat_seq"]),
            "max_tokens": values["max_tokens"],
            "budget_hit": values["i"] + 1 >= values["max_tokens"],
            "evidence": "native generator stop head at nonstreaming yield",
        }
    return {"status": "not_exposed_by_model_api"}


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def download(names: list[str]) -> None:
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    for name in names:
        repo, revision = REPOS[name]
        path = BASE / "weights" / name
        snapshot_download(
            repo,
            revision=revision,
            local_dir=path,
            ignore_patterns=["*.pt", "samples/*", "test_*.wav"],
            allow_patterns=["int4/*", "README.md"] if name == "dots-soar" else None,
        )
        files = [
            {"path": str(p.relative_to(path)), "bytes": p.stat().st_size, "sha256": digest(p)}
            for p in sorted(path.rglob("*"))
            if p.is_file() and ".cache" not in p.parts
        ]
        save(OUT / f"{name}-download.json", {"repo": repo, "revision": revision, "files": files})


def run(name: str, seed: int, *, output_dir: Path | None = None) -> None:
    family, settings = CANDIDATES[name]
    settings = dict(settings)
    folder = output_dir if output_dir is not None else OUT / name
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise SystemExit("Evidence already exists; use a new output directory.") from error

    import numpy as np  # type: ignore[import-not-found]
    import psutil
    import soundfile as sf  # type: ignore[import-not-found]

    report: dict[str, Any] = {
        "candidate": name,
        "seed": seed,
        "script": str(SCRIPT.relative_to(ROOT)),
        "script_sha256": digest(SCRIPT),
        "runner_sha256": digest(Path(__file__)),
        "settings": settings,
        "postprocessing": [],
        "segmentation": (
            "identical nonempty paragraphs; native segments concatenated without inserted silence"
        ),
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "runtime_python": sys.version,
        "segments": [],
        "human_listening": "pending",
        "segment_deadline_seconds": 480,
    }
    save(folder / "report.json", {**report, "status": "waiting_for_shared_gpu_lock"})
    with Path("/tmp/whoopy-benchmark-gpu.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Allow the previous process's Metal allocations to settle after exit.
        # Keep the same conservative threshold and headroom-based allocator cap.
        time.sleep(2)
        report["post_lock_settle_seconds"] = 2
        available = psutil.virtual_memory().available
        report["available_memory_before_bytes"] = available
        try:
            report["swap_used_before_bytes"] = int(psutil.swap_memory().used)
        except Exception as error:
            save(
                folder / "report.json",
                {**report, "status": "preflight_failed", "error": f"swap observer: {error}"},
            )
            return
        minimum_available = (16 if name == "mlx-qwen-custom-ryan" else 8) * 1024**3
        report["minimum_available_bytes"] = minimum_available
        if available < minimum_available:
            save(
                folder / "report.json",
                {
                    **report,
                    "status": "preflight_failed",
                    "error": f"less than {minimum_available // 1024**3} GiB available",
                },
            )
            return
        save(folder / "report.json", {**report, "status": "loading"})
        try:
            watchdog = ResourceWatchdog(folder, psutil)
            watchdog.start()
        except Exception as error:
            save(
                folder / "report.json",
                {**report, "status": "preflight_failed", "error": f"resource observer: {error}"},
            )
            return
        try:
            signal.signal(signal.SIGALRM, segment_timeout)
            np.random.seed(seed)
            load_start = time.monotonic()
            if family.startswith("qwen"):
                import torch  # type: ignore[import-not-found]
                from qwen_tts import Qwen3TTSModel  # type: ignore[import-not-found]

                task = "custom-voice" if family == "qwen-custom" else "voice-design"
                path = ROOT / f"models/managed/model-packs/qwen3-tts-{task}-1.7b/files"
                torch.manual_seed(seed)
                model = Qwen3TTSModel.from_pretrained(
                    str(path),
                    device_map="mps",
                    dtype=torch.float16,
                    attn_implementation="eager",
                    local_files_only=True,
                )
                report.update(device="mps", model_path=str(path.relative_to(ROOT)))
                report["model_revision"] = (
                    "0c0e3051f131929182e2c023b9537f8b1c68adfe"
                    if family == "qwen-custom"
                    else "5ecdb67327fd37bb2e042aab12ff7391903235d3"
                )
                report["generation_policy"] = {
                    "do_sample": True,
                    "subtalker_dosample": True,
                    "max_new_tokens_policy": "text_units_x12_plus96_clamped_192_2048@1",
                    "fallback": "none; failures recorded",
                }
                completion: dict[str, Any] = {}
                talker_generate = model.model.talker.generate

                def tracked_talker(*args: Any, **kwargs: Any) -> Any:
                    result = talker_generate(*args, **kwargs)
                    codes = torch.stack(
                        [h[-1] for h in result.hidden_states if h[-1] is not None], dim=1
                    )
                    eos = model.model.config.talker_config.codec_eos_token_id
                    completion.update(
                        codec_steps=int(codes.shape[1]),
                        codec_eos_observed=bool((codes[:, :, 0] == eos).any().item()),
                        max_new_tokens=int(kwargs["max_new_tokens"]),
                    )
                    return result

                model.model.talker.generate = tracked_talker
            else:
                import mlx.core as mx  # type: ignore[import-not-found]
                from mlx_audio.tts.utils import load_model  # type: ignore[import-not-found]

                if family == "chatterbox-v3":
                    # Upstream's post-load hook requests this codec at main.
                    # Resolve only that known dependency to our pinned download.
                    import huggingface_hub

                    original_download = huggingface_hub.snapshot_download

                    def pinned_dependency(*args: Any, **kwargs: Any) -> Any:
                        repo = kwargs.get("repo_id", args[0] if args else None)
                        if repo == "mlx-community/S3TokenizerV2":
                            return str(BASE / "weights/s3tokenizer")
                        return original_download(*args, **kwargs)

                    huggingface_hub.snapshot_download = pinned_dependency
                    report["codec"] = {
                        "repo": REPOS["s3tokenizer"][0],
                        "revision": REPOS["s3tokenizer"][1],
                        "resolution": "benchmark-local pinned dependency resolver",
                    }
                mx.random.seed(seed)
                # A finite allocator cap preserves headroom; never disable safety.
                allocator_limit = min(24 * 1024**3, max(1024**3, available - 6 * 1024**3))
                report["allocator_limit_bytes"] = allocator_limit
                mx.set_memory_limit(allocator_limit)
                mx.reset_peak_memory()
                path = BASE / "weights" / family
                if family == "dots-soar":
                    import dots_tts_mlx.loader as dots_loader  # type: ignore[import-not-found]

                    # That module's import sets 45GiB; immediately restore our
                    # measured headroom-based lower cap before loading weights.
                    mx.set_memory_limit(allocator_limit)
                    model = dots_loader.from_pretrained(path / "int4", dtype=mx.bfloat16).model
                else:
                    model = load_model(str(path))
                report.update(
                    device="mlx-metal",
                    model_repo=REPOS[family][0],
                    model_revision=REPOS[family][1],
                    mlx_audio_revision=MLX_REVISION,
                )
                if family in {"chatterbox-v3", "moss-local", "dots-soar"}:
                    reference = OUT / "voxcpm2-reference/raw.wav"
                    if not reference.exists():
                        raise FileNotFoundError(
                            "Generate deliberate synthetic VoxCPM2 reference first"
                        )
                    report["synthetic_reference"] = {
                        "source": "voxcpm2-reference native generated benchmark reference",
                        "transcript": "Settle into a position that feels comfortable.",
                        "sha256": digest(reference),
                        "real_person": False,
                    }
                    if family == "dots-soar":
                        # Upstream's wave reader supports PCM, not IEEE FLOAT.
                        # Preserve the native synthetic source; quantize only a
                        # declared conditioning copy, never generated outputs.
                        ref_samples, ref_rate = sf.read(reference, dtype="float32")
                        if float(np.max(np.abs(ref_samples))) >= 1:
                            raise ValueError("PCM16 reference conversion would clip")
                        pcm_reference = folder / "conditioning-reference-pcm16.wav"
                        sf.write(pcm_reference, ref_samples, ref_rate, subtype="PCM_16")
                        report["synthetic_reference"]["conditioning_copy"] = {
                            "path": str(pcm_reference.relative_to(ROOT)),
                            "sha256": digest(pcm_reference),
                            "transformation": "FLOAT to PCM16 quantization only",
                            "sample_rate": ref_rate,
                            "frames": len(ref_samples),
                            "gain": 1.0,
                            "runtime_input_preprocessing": (
                                "native Dots enrollment trims reference silence at top_db30 "
                                "and loads mono48k"
                            ),
                        }
                        t = time.monotonic()
                        profile = model.enroll(
                            pcm_reference,
                            report["synthetic_reference"]["transcript"],
                            speaker_scale=1.5,
                        )
                        profile.save(folder / "synthetic-male.dtprofile")
                        report["reference_enrollment_seconds"] = time.monotonic() - t
                    else:
                        settings["ref_audio"] = str(reference)
                    if family == "moss-local":
                        codec = ROOT / "models/managed/model-packs/moss-audio-tokenizer-v2/files"
                        if not codec.exists():
                            codec = (
                                ROOT
                                / "models/experimental/moss-tts-runtime/checkpoints"
                                / "MOSS-Audio-Tokenizer-v2"
                            )
                        settings["audio_tokenizer_source"] = str(codec)
                        report["codec"] = {
                            "repo": "OpenMOSS-Team/MOSS-Audio-Tokenizer-v2",
                            "revision": "f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169",
                            "local_path": str(codec.relative_to(ROOT)),
                            "config_sha256": digest(codec / "config.json"),
                        }
            report["load_seconds"] = time.monotonic() - load_start
            save(folder / "report.json", {**report, "status": "generating"})
            all_audio = []
            sample_rate = None
            synth_total = 0.0
            paragraphs = [p.strip() for p in SCRIPT.read_text().split("\n\n") if p.strip()]
            if name == "voxcpm2-reference":
                paragraphs = ["Settle into a position that feels comfortable."]
                report["scope"] = (
                    "synthetic conditioning asset only; "
                    "excluded from fixed-script audition comparison"
                )
            for i, text in enumerate(paragraphs, 1):
                t = time.monotonic()
                signal.alarm(480)
                stop_evidence: dict[str, Any] = {"status": "not_exposed_by_model_api"}
                if family.startswith("qwen"):
                    torch.manual_seed(seed + i - 1)
                    method = (
                        model.generate_custom_voice
                        if family == "qwen-custom"
                        else model.generate_voice_design
                    )
                    waves, sr = method(
                        text=text,
                        language="English",
                        max_new_tokens=qwen_token_budget(text),
                        do_sample=True,
                        subtalker_dosample=True,
                        **settings,
                    )
                    audio = np.asarray(waves[0], dtype=np.float32).squeeze()
                    torch.mps.synchronize()
                    stop_evidence = dict(completion)
                elif family == "dots-soar":
                    result = model.generate(
                        text=text, profile=profile, seed=seed + i - 1, **settings
                    )
                    mx.eval(result["audio"])
                    audio = np.asarray(result["audio"][0], dtype=np.float32)
                    sr = int(result["sample_rate"])
                else:
                    mx.random.seed(seed + i - 1)
                    chunks = []
                    segment_settings = dict(settings)
                    if family.startswith("mlx-qwen-"):
                        segment_settings["max_tokens"] = qwen_token_budget(text)
                    iterator = model.generate(text=text, **segment_settings)
                    for result in iterator:
                        mx.eval(result.audio)
                        chunks.append(np.asarray(result.audio, dtype=np.float32))
                        sr = int(result.sample_rate)
                        stop_evidence = generator_stop_evidence(iterator, family)
                    if not chunks:
                        raise RuntimeError("no model output")
                    audio = np.concatenate(chunks, axis=0)
                elapsed = time.monotonic() - t
                signal.alarm(0)
                if audio.ndim > 1 and audio.shape[0] <= 2:
                    audio = audio.T
                if audio.size == 0 or not np.isfinite(audio).all():
                    raise RuntimeError("invalid model waveform")
                if sample_rate is not None and sample_rate != sr:
                    raise RuntimeError("native sample rate changed")
                sample_rate = sr
                out = folder / f"segment-{i:02}.wav"
                sf.write(out, audio, sr, subtype="FLOAT")
                duration = audio.shape[0] / sr
                report["segments"].append(
                    {
                        "text": text,
                        "seed": seed + i - 1,
                        "path": str(out.relative_to(ROOT)),
                        "sha256": digest(out),
                        "duration_seconds": duration,
                        "synthesis_seconds": elapsed,
                        "rtf": elapsed / duration,
                        "stop_evidence": stop_evidence,
                    }
                )
                synth_total += elapsed
                all_audio.append(audio)
                save(folder / "report.json", {**report, "status": "generating"})
                if family.startswith("qwen") and not completion.get("codec_eos_observed"):
                    raise RuntimeError(f"Qwen output has no observed codec EOS: {completion}")
                if family.startswith("mlx-qwen-") and not stop_evidence.get("codec_eos_observed"):
                    raise RuntimeError(f"MLX Qwen has no observed codec EOS: {stop_evidence}")
            assembled = np.concatenate(all_audio, axis=0)
            raw = folder / "raw.wav"
            sf.write(raw, assembled, sample_rate, subtype="FLOAT")
            duration = assembled.shape[0] / sample_rate
            report.update(
                status="complete",
                raw_path=str(raw.relative_to(ROOT)),
                raw_sha256=digest(raw),
                sample_rate=sample_rate,
                duration_seconds=duration,
                synthesis_seconds=synth_total,
                speech_output_rtf=synth_total / duration,
                process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                allocator_peak_bytes=int(mx.get_peak_memory())
                if not family.startswith("qwen")
                else None,
                mps_current_allocated_bytes=int(torch.mps.current_allocated_memory())
                if family.startswith("qwen")
                else None,
                mps_driver_allocated_bytes=int(torch.mps.driver_allocated_memory())
                if family.startswith("qwen")
                else None,
                memory_note=(
                    "macOS ru_maxrss bytes; MLX allocator peak separately; "
                    "MPS API has no peak counter here"
                ),
            )
        except (Exception, KeyboardInterrupt) as e:
            signal.alarm(0)
            report.update(
                status="incomplete_memory_pressure"
                if watchdog.reason
                else "interrupted"
                if isinstance(e, KeyboardInterrupt)
                else "failed",
                error=f"{type(e).__name__}: {e}",
                traceback=traceback.format_exc(),
                process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            )
            traceback.print_exc()
        finally:
            signal.alarm(0)
            report["resource_watchdog"] = watchdog.finish()
            report["process_peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if not family.startswith("qwen") and "mx" in locals():
                report["allocator_peak_bytes"] = int(mx.get_peak_memory())
        if watchdog.reason:
            report["status"] = "incomplete_memory_pressure"
            report["error"] = watchdog.reason
        save(folder / "report.json", report)
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in {"packages", "segments", "traceback"}},
                indent=2,
            )
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["download", "run"])
    p.add_argument("names", nargs="+")
    p.add_argument("--seed", type=int, default=20261028)
    p.add_argument("--output-dir", type=Path, help="fresh candidate folder for a run")
    args = p.parse_args()
    if args.action == "download":
        download(args.names)
    else:
        if len(args.names) != 1:
            p.error("run exactly one candidate per process")
        run(args.names[0], args.seed, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
