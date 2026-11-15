from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from whoopy.adapters.tts.mlx_installation import MlxQwenInstallation
from whoopy.audio.synthesis import FatalSynthesisError
from whoopy.hardware import HardwareSnapshot
from whoopy.pipeline import RunStore
from whoopy.webui.api import GenerationRequest, create_api
from whoopy.webui.server import LocalWebApplication

CUSTOM = "mlx-qwen3-custom-voice-1.7b-8bit"
DESIGN = "mlx-qwen3-voice-design-1.7b-8bit"
PROJECT = Path(__file__).resolve().parents[1]


def snapshot(**changes: Any) -> HardwareSnapshot:
    return HardwareSnapshot.model_validate(
        {
            "operating_system": "darwin",
            "architecture": "arm64",
            "cpu_count": 8,
            "total_ram_gb": 48,
            "available_ram_gb": 30,
            "free_disk_gb": 100,
            "accelerators": ["metal"],
            **changes,
        }
    )


def payload(backend: str, **changes: Any) -> dict[str, Any]:
    return {
        "surface": "lab",
        "mode": "prompt",
        "text": "A seated grounding practice using ordinary sounds.",
        "minutes": 3,
        "profile": "high",
        "writing_method": "recipe_whole_session",
        "tts_model": backend,
        "qwen_language": "English",
        **(
            {"qwen_speaker": "Ryan"}
            if backend == CUSTOM
            else {"qwen_voice_design_preset": "grounded_meditation"}
        ),
        **changes,
    }


class EmptyPacks:
    def list(self) -> dict[str, Any]:
        return {"packs": []}


@pytest.fixture
def bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalWebApplication, list[str], list[list[str]], threading.Event]:
    verified: list[str] = []
    commands: list[list[str]] = []
    launched = threading.Event()

    def resolve(root: Path, backend: str, *, manifest_path: Path) -> MlxQwenInstallation:
        assert root == tmp_path / "models"
        assert manifest_path == PROJECT / "config/experimental_tts.yaml"
        verified.append(backend)
        return MlxQwenInstallation(
            runtime_directory=root / "experimental",
            model_directory=root / "checkpoint",
            python_executable=root / "python",
            runtime_revision="runtime-test",
            model_revision="model-test",
            installation_sha256="a" * 64,
            backend=backend,
            task="custom_voice" if backend == CUSTOM else "voice_design",
            repository="local-test",
        )

    class Process:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            commands.append(command)
            launched.set()

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr("whoopy.webui.server.inspect_hardware", lambda _path: snapshot())
    monkeypatch.setattr("whoopy.webui.server.resolve_mlx_qwen_installation", resolve)
    application = LocalWebApplication(
        project_root=PROJECT,
        config_directory=PROJECT / "config",
        models_directory=tmp_path / "models/managed",
        runs_directory=tmp_path / "runs",
        command_launcher=Process,
        model_pack_service_factory=lambda _registry, _models: EmptyPacks(),
    )
    return application, verified, commands, launched


@pytest.mark.parametrize("backend", [CUSTOM, DESIGN])
def test_mlx_api_launch_uses_exact_writer_and_voice_controls(
    bridge: tuple[LocalWebApplication, list[str], list[list[str]], threading.Event], backend: str
) -> None:
    application, verified, commands, launched = bridge
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/generate", json=payload(backend))
    assert response.status_code == 202, response.text
    assert launched.wait(2)
    command = commands[0]
    expected = {
        "--tts-model": backend,
        "--qwen-language": "English",
        "--profile": "high",
        "--writing-method": "recipe_whole_session",
        "--audience": "lab",
        "--qwen-speaker" if backend == CUSTOM else "--qwen-voice-design-preset": (
            "Ryan" if backend == CUSTOM else "grounded_meditation"
        ),
    }
    for flag, value in expected.items():
        assert command[command.index(flag) + 1] == value
    assert "--speed" not in command and "--voice" not in command
    assert "--moss-language" not in command and "--moss-instruction" not in command
    assert ("--qwen-voice-design-preset" if backend == CUSTOM else "--qwen-speaker") not in command
    assert verified == [backend]
    record = RunStore(application.runs_directory).load(response.json()["run_id"])
    assert record.audience == "lab"


@pytest.mark.parametrize(
    ("backend", "changes", "message"),
    [
        (CUSTOM, {"surface": "studio"}, "Lab-only"),
        (CUSTOM, {"qwen_language": "Japanese"}, "only English"),
        (CUSTOM, {"qwen_speaker": "Vivian"}, "Ryan, Aiden, or Serena"),
        (CUSTOM, {"qwen_voice_design_preset": "warm_meditation"}, "preset speaker"),
        (CUSTOM, {"qwen_reference": "owned"}, "references"),
        (CUSTOM, {"speed": 0.8}, "Numeric speed"),
        (CUSTOM, {"seed": 2**32}, "32-bit"),
        (DESIGN, {"qwen_speaker": "Ryan"}, "design preset"),
        (DESIGN, {"qwen_voice_design_preset": "arbitrary"}, "Input should be"),
        (DESIGN, {"moss_instruction": "free form"}, "MOSS controls"),
    ],
)
def test_mlx_typed_contract_rejects_inactive_or_unsupported_controls(
    backend: str, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        GenerationRequest.model_validate(payload(backend, **changes))


def test_mlx_status_is_experimental_and_cached_but_launch_reverifies(
    bridge: tuple[LocalWebApplication, list[str], list[list[str]], threading.Event],
) -> None:
    application, verified, _commands, launched = bridge
    first = application.environment_status()
    assert first["ok"] is True
    assert verified == [CUSTOM, DESIGN]
    for backend in (CUSTOM, DESIGN):
        model = first["speech_models"][backend]
        assert model["ready"] is True
        assert model["managed_ready"] is False
        assert model["studio_eligible"] is False
        assert model["lab_only"] is True
        assert model["classification"] == "experimental_local"
        assert model["state"] == "experimental_available"
        assert model["capabilities"]["languages"] == ["English"]
        assert "speed" not in model["capabilities"]["request_fields"]
    assert first["speech_models"][CUSTOM]["capabilities"]["preset_voices"] == [
        "Ryan",
        "Aiden",
        "Serena",
    ]
    assert application.environment_status() == first
    assert verified == [CUSTOM, DESIGN]
    application.start_generation(payload(CUSTOM))
    assert launched.wait(2)
    assert verified == [CUSTOM, DESIGN, CUSTOM]


@pytest.mark.parametrize(
    ("hardware", "message"),
    [
        ({"operating_system": "linux"}, "Apple Silicon"),
        ({"architecture": "x86_64"}, "Apple Silicon"),
        ({"available_ram_gb": 15.9}, "16 GiB"),
    ],
)
def test_mlx_host_and_memory_preflight_precedes_hashing_and_enqueue(
    bridge: tuple[LocalWebApplication, list[str], list[list[str]], threading.Event],
    monkeypatch: pytest.MonkeyPatch,
    hardware: dict[str, Any],
    message: str,
) -> None:
    application, verified, commands, _launched = bridge
    monkeypatch.setattr("whoopy.webui.server.inspect_hardware", lambda _path: snapshot(**hardware))
    with pytest.raises(ValueError, match=message):
        application.start_generation(payload(CUSTOM))
    assert verified == []
    assert commands == []
    assert application.list_runs() == []
    status = application.environment_status()["speech_models"][CUSTOM]
    assert status["ready"] is False
    assert message in status["error"]
    assert verified == []


def test_mlx_installation_failure_is_an_ordinary_request_error(
    bridge: tuple[LocalWebApplication, list[str], list[list[str]], threading.Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _verified, commands, _launched = bridge

    def drift(*_args: Any, **_kwargs: Any) -> Any:
        raise FatalSynthesisError("Pinned installation changed.")

    monkeypatch.setattr("whoopy.webui.server.resolve_mlx_qwen_installation", drift)
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/generate", json=payload(CUSTOM))
    assert response.status_code == 400
    assert response.json()["error"] == "Pinned installation changed."
    assert commands == []
    assert application.list_runs() == []


@pytest.mark.parametrize("backend", [CUSTOM, DESIGN])
def test_mlx_unlaunched_browser_request_resumes_with_exact_durable_envelope(
    bridge: tuple[LocalWebApplication, list[str], list[list[str]], threading.Event],
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    application, verified, _commands, _launched = bridge
    pending: list[threading.Thread] = []
    monkeypatch.setattr(threading.Thread, "start", lambda thread: pending.append(thread))

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("stub worker not available")

    monkeypatch.setattr(application, "_command_launcher", unavailable)
    run_id = application.start_generation(payload(backend))["run_id"]
    pending.pop().run()
    saved = application._load_web_launch_request(run_id)
    resumed = application.resume_run(run_id)
    assert resumed is not None and resumed["run_id"] == run_id
    pending.pop().run()
    assert application._load_web_launch_request(run_id) == saved
    assert verified == [backend, backend]
    history = application.runs_directory / run_id / "history/web-launch-attempts"
    assert len(list(history.glob("*.json"))) == 1


@pytest.mark.parametrize("backend", ["fish-1.4", "moss-local-v1.5", CUSTOM, DESIGN])
@pytest.mark.parametrize("route", ["/api/generate", "/api/v1/generate"])
def test_non_kokoro_numeric_speed_is_rejected_before_dispatch(
    bridge: tuple[LocalWebApplication, list[str], list[list[str]], threading.Event],
    backend: str,
    route: str,
) -> None:
    application, verified, commands, _launched = bridge
    body = {
        "surface": "lab",
        "mode": "script",
        "text": "Notice a sound.",
        "tts_model": backend,
        "speed": 0.8,
    }
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.post(route, json=body)
    assert response.status_code == 422
    assert "Numeric speed" in response.text
    with pytest.raises(ValueError, match="Numeric speed"):
        application.start_generation(body)
    assert verified == []
    assert commands == []
    assert application.list_runs() == []
