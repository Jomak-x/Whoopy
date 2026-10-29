from __future__ import annotations

import math
import time
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest

from whoopy.audio.synthesis import FatalSynthesisError
from whoopy.evaluation import EvaluationRepository, PromotionDecision
from whoopy.voice_references import VoiceConsent
from whoopy.webui.api import GenerationRequest, _studio_voice_promotion_error
from whoopy.webui.catalog import LocalCatalog
from whoopy.webui.server import LocalWebApplication


def _declared_installation(models_root: Path) -> None:
    runtime = models_root / "managed/runtimes/qwen3-tts"
    model = models_root / "managed/model-packs/qwen3-tts-custom-voice-0.6b/files"
    runtime.mkdir(parents=True)
    model.mkdir(parents=True)
    (runtime / ".whoopy-runtime.json").write_text(
        '{"schema_version":1,"runtime_id":"qwen3-tts","revision":"runtime-test",'
        '"upstream_revision":"upstream-test","target":"darwin-arm64",'
        '"bundle_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"installed_tree_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        '"installed_at":"2026-08-05T00:00:00Z"}',
        encoding="utf-8",
    )
    (model / ".whoopy-model.json").write_text(
        ('{"schema_version":1,"variant":"custom-voice-0.6b","model_revision":"model-test"}'),
        encoding="utf-8",
    )


class _FakePacks:
    def list(self) -> dict[str, object]:
        return {"packs": []}


class _ReadyQwenPacks:
    def __init__(self, pack_id: str = "qwen3-tts-custom-voice-0.6b") -> None:
        self.pack_id = pack_id

    def list(self) -> dict[str, object]:
        return {
            "packs": [
                {
                    "pack_id": self.pack_id,
                    "state": "ready",
                    "checks": [
                        {"check": "pinned_files", "passed": True, "message": "verified"},
                        {"check": "runtime_probe", "passed": True, "message": "passed"},
                        {
                            "check": "offline_smoke_test",
                            "passed": True,
                            "message": "rendered",
                        },
                        {
                            "check": "playable_smoke_sample",
                            "passed": True,
                            "message": "playable",
                        },
                    ],
                }
            ]
        }


def _managed_reference(application: LocalWebApplication, source: Path) -> str:
    rate = 24_000
    samples = array(
        "h",
        (round(4_000 * math.sin(2 * math.pi * 220 * index / rate)) for index in range(rate * 2)),
    )
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())
    record = application.voice_reference_manager.import_wav(
        reference_id="owned-voice",
        display_name="Owned voice",
        source_wav=source,
        transcript="This is my consented voice reference.",
        consent=VoiceConsent(
            confirmed=True,
            scope="local_voice_cloning_experiment_only",
            source_kind="user_provided",
            rights_attestation="I own this voice or have permission to clone it.",
            recorded_at=datetime(2026, 8, 5, tzinfo=UTC),
        ),
    )
    return record.reference_id


def _record_voice_promotion(
    application: LocalWebApplication,
    *,
    model_id: str,
    voice_id: str,
    language: str = "Japanese",
) -> None:
    decision_id = uuid4()
    rating_id = uuid4()
    report_sha256 = "a" * 64
    selected_value = f"{model_id}|{voice_id}|reference|neutral|subtle|{language}"
    decision = PromotionDecision(
        decision_id=decision_id,
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
        experiment_id="qwen-base-promotion-test",
        experiment_version=1,
        outcome="promote",
        studio_target="voice",
        candidate_id="base-candidate",
        previous_value="kokoro|af_heart|preset|neutral|subtle|English",
        selected_value=selected_value,
        rationale="Validated unit-test promotion evidence.",
        report_sha256=report_sha256,
        rating_ids=[rating_id],
        rating_sha256_by_id={str(rating_id): "b" * 64},
    )
    EvaluationRepository(application.runs_directory / ".whoopy-lab").save_decision(decision)
    LocalCatalog(application.runs_directory / ".whoopy-catalog.sqlite3").set_setting(
        "studio.default.voice",
        {
            "schema_version": 2,
            "value": selected_value,
            "candidate_id": decision.candidate_id,
            "selection_role": None,
            "decision_id": str(decision_id),
            "experiment_id": decision.experiment_id,
            "experiment_version": decision.experiment_version,
            "report_sha256": report_sha256,
            "selected_condition": {
                "voice": {
                    "model_id": model_id,
                    "voice_id": voice_id,
                    "voice_mode": "reference",
                    "delivery": "neutral",
                    "intensity": "subtle",
                    "language": language,
                }
            },
            "promoted_at": decision.created_at.isoformat(),
        },
    )


@pytest.mark.parametrize(
    ("model_id", "pack_id"),
    [
        ("qwen3-base-clone-1.7b", "qwen3-tts-base-clone-1.7b"),
    ],
)
def test_base_lab_launch_routes_exact_variant_language_and_managed_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    pack_id: str,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    captured: dict[str, list[str]] = {}

    class CapturedProcess:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured["command"] = command

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(
        "whoopy.webui.server.resolve_qwen3_tts_installation",
        lambda *_args, **_kwargs: None,
    )
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        command_launcher=CapturedProcess,
        model_pack_service_factory=lambda _registry, _models: _ReadyQwenPacks(pack_id),
    )
    reference_id = _managed_reference(application, tmp_path / "reference.wav")

    application.start_generation(
        {
            "surface": "lab",
            "mode": "script",
            "text": "Notice this moment.",
            "tts_model": model_id,
            "qwen_language": "Japanese",
            "qwen_reference": reference_id,
            "delivery": "neutral",
            "intensity": "subtle",
            "pace": "natural",
        }
    )
    deadline = time.monotonic() + 2
    while "command" not in captured and time.monotonic() < deadline:
        time.sleep(0.01)
    command = captured["command"]

    assert command[command.index("--tts-model") + 1] == model_id
    assert command[command.index("--qwen-language") + 1] == "Japanese"
    assert command[command.index("--qwen-reference") + 1] == reference_id
    assert "--qwen-speaker" not in command
    assert "--qwen-voice-design-preset" not in command


@pytest.mark.parametrize("model_id", ["qwen3-base-clone-0.6b", "qwen3-base-clone-1.7b"])
def test_base_clone_rejects_controls_outside_its_typed_contract(
    model_id: Literal["qwen3-base-clone-0.6b", "qwen3-base-clone-1.7b"],
) -> None:
    from pydantic import ValidationError

    from whoopy.webui.api import GenerationRequest

    with pytest.raises(ValidationError, match="neutral delivery"):
        GenerationRequest(
            surface="lab",
            mode="script",
            text="Notice this moment.",
            tts_model=model_id,
            qwen_reference="owned-voice",
            delivery="calm",
            pace="slow",
        )


def test_ready_base_with_matching_immutable_promotion_is_selectable_and_launches_in_studio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    captured: dict[str, list[str]] = {}

    class CapturedProcess:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured["command"] = command

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(
        "whoopy.webui.server.resolve_qwen3_tts_installation",
        lambda *_args, **_kwargs: None,
    )
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        command_launcher=CapturedProcess,
        model_pack_service_factory=lambda _registry, _models: _ReadyQwenPacks(
            "qwen3-tts-base-clone-1.7b"
        ),
    )
    reference_id = _managed_reference(application, tmp_path / "reference.wav")
    _record_voice_promotion(
        application,
        model_id="qwen3-base-clone-1.7b",
        voice_id=reference_id,
    )

    status = application.environment_status()["speech_models"]["qwen3-base-clone-1.7b"]
    assert status["studio_eligible"] is True
    assert status["lab_only"] is False
    assert status["promotion_status"] == "accepted"

    request = GenerationRequest(
        surface="studio",
        mode="script",
        text="Notice this moment.",
        tts_model="qwen3-base-clone-1.7b",
        qwen_language="Japanese",
        qwen_reference=reference_id,
        delivery="neutral",
        intensity="subtle",
        pace="natural",
    )
    with pytest.raises(ValueError, match="exact accepted Lab voice decision"):
        application.start_generation(
            request.model_copy(update={"qwen_language": "English"}).model_dump(exclude_none=True)
        )
    application.start_generation(request.model_dump(exclude_none=True))
    deadline = time.monotonic() + 2
    while "command" not in captured and time.monotonic() < deadline:
        time.sleep(0.01)
    assert captured["command"][captured["command"].index("--audience") + 1] == "studio"


def test_qwen_promotion_check_fails_closed_for_tampered_or_missing_decision(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        model_pack_service_factory=lambda _registry, _models: _ReadyQwenPacks(
            "qwen3-tts-base-clone-1.7b"
        ),
    )
    LocalCatalog(application.runs_directory / ".whoopy-catalog.sqlite3").set_setting(
        "studio.default.voice",
        {
            "schema_version": 2,
            "value": "qwen3-base-clone-1.7b|owned-voice|reference|neutral|subtle|English",
            "decision_id": str(uuid4()),
            "experiment_id": "missing",
            "experiment_version": 1,
            "candidate_id": "base-candidate",
            "report_sha256": "a" * 64,
            "selected_condition": {"voice": {"model_id": "qwen3-base-clone-1.7b"}},
        },
    )

    status = application.environment_status()["speech_models"]["qwen3-base-clone-1.7b"]
    assert status["studio_eligible"] is False
    assert status["lab_only"] is True
    with pytest.raises(ValueError, match="Lab-only"):
        application._require_speech_model_ready("qwen3-base-clone-1.7b", surface="studio")

    _record_voice_promotion(
        application,
        model_id="qwen3-base-clone-1.7b",
        voice_id="owned-voice",
        language="English",
    )
    catalog = LocalCatalog(application.runs_directory / ".whoopy-catalog.sqlite3")
    setting = catalog.export_settings()["studio.default.voice"]
    setting["selected_condition"]["voice"]["language"] = "Japanese"
    catalog.set_setting("studio.default.voice", setting)
    assert application._qwen_has_current_studio_promotion("qwen3-base-clone-1.7b") is False


def test_local_application_refuses_qwen_without_current_production_smoke(tmp_path: Path) -> None:
    _declared_installation(tmp_path / "models")
    project_root = Path(__file__).resolve().parents[1]
    captured: dict[str, list[str]] = {}

    class CapturedProcess:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured["command"] = command

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        command_launcher=CapturedProcess,
    )

    with pytest.raises(FatalSynthesisError, match=r"is missing;.*pinned files"):
        application.start_generation(
            {
                "surface": "lab",
                "mode": "script",
                "text": "Notice the support beneath you.",
                "tts_model": "qwen3-custom-voice-0.6b",
                "qwen_language": "Japanese",
                "qwen_speaker": "Ono_Anna",
                "delivery": "calm",
            }
        )
    assert "command" not in captured


def test_qwen_status_rejects_unverified_local_declarations(tmp_path: Path) -> None:
    _declared_installation(tmp_path / "models")
    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        model_pack_service_factory=lambda _registry, _models: _FakePacks(),
    )

    status = application.environment_status()
    qwen = status["speech_models"]["qwen3-custom-voice-0.6b"]

    assert status["ok"] is True
    assert qwen["state"] == "missing"
    assert qwen["ready"] is False
    assert qwen["studio_eligible"] is False
    assert qwen["lab_only"] is True
    assert "pinned files" in qwen["error"]
    assert qwen["capabilities"]["voice_modes"] == ["preset"]
    assert qwen["capabilities"]["delivery"] == {
        "values": ["neutral"],
        "fallback": "neutral",
    }
    assert qwen["capabilities"]["intensity"] == {
        "values": ["subtle"],
        "fallback": "subtle",
    }
    assert qwen["capabilities"]["pace"]["values"] == ["natural"]
    assert qwen["capabilities"]["supports_freeform_instruction"] is False
    assert qwen["capabilities"]["request_fields"] == [
        "qwen_language",
        "qwen_speaker",
        "delivery",
        "intensity",
        "pace",
    ]


def test_studio_cannot_launch_a_lab_only_qwen_declaration(tmp_path: Path) -> None:
    _declared_installation(tmp_path / "models")
    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )

    with pytest.raises(ValueError, match="Lab-only"):
        application.start_generation(
            {
                "mode": "script",
                "text": "Notice the support beneath you.",
                "tts_model": "qwen3-custom-voice-0.6b",
            }
        )


def test_current_qwen_smoke_remains_blocked_after_eos_quality_failure(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        model_pack_service_factory=lambda _registry, _models: _ReadyQwenPacks(),
    )

    qwen = application.environment_status()["speech_models"]["qwen3-custom-voice-0.6b"]

    assert qwen["state"] == "experimental"
    assert qwen["ready"] is False
    assert qwen["readiness"]["installed_files"]["passed"] is True
    assert qwen["readiness"]["runtime_probe"]["passed"] is True
    assert qwen["readiness"]["playable_synthesis"]["passed"] is False
    assert "codec-token limit" in qwen["error"]
    assert qwen["lab_only"] is True
    assert qwen["studio_eligible"] is False
    with pytest.raises(ValueError, match="natural ending"):
        application._require_speech_model_ready("qwen3-custom-voice-0.6b", surface="lab")
    with pytest.raises(ValueError, match="Lab-only"):
        application._require_speech_model_ready("qwen3-custom-voice-0.6b", surface="studio")


def test_qwen_quality_blocker_is_visible_even_when_smoke_evidence_is_stale(
    tmp_path: Path,
) -> None:
    class InstalledQwenPacks:
        def list(self) -> dict[str, object]:
            return {
                "packs": [
                    {
                        "pack_id": "qwen3-tts-custom-voice-0.6b",
                        "state": "installed",
                        "checks": [
                            {"check": "pinned_files", "passed": True, "message": "verified"},
                            {
                                "check": "runtime_probe",
                                "passed": False,
                                "message": "stale evidence",
                            },
                        ],
                    }
                ]
            }

    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        model_pack_service_factory=lambda _registry, _models: InstalledQwenPacks(),
    )

    qwen = application.environment_status()["speech_models"]["qwen3-custom-voice-0.6b"]

    assert qwen["state"] == "experimental"
    assert qwen["ready"] is False
    assert "natural ending" in qwen["error"]
    assert qwen["readiness"]["playable_synthesis"]["passed"] is False


def test_base_clone_0_6b_smoke_remains_blocked_after_product_eos_failure(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        model_pack_service_factory=lambda _registry, _models: _ReadyQwenPacks(
            "qwen3-tts-base-clone-0.6b"
        ),
    )

    qwen = application.environment_status()["speech_models"]["qwen3-base-clone-0.6b"]

    assert qwen["state"] == "experimental"
    assert qwen["ready"] is False
    assert qwen["readiness"]["installed_files"]["passed"] is True
    assert qwen["readiness"]["runtime_probe"]["passed"] is True
    assert qwen["readiness"]["playable_synthesis"]["passed"] is False
    assert "15.28 seconds" in qwen["error"]
    assert "Base Clone 1.7B" in qwen["error"]
    assert qwen["lab_only"] is True
    assert qwen["studio_eligible"] is False
    with pytest.raises(ValueError, match="natural ending"):
        application._require_speech_model_ready("qwen3-base-clone-0.6b", surface="lab")
    with pytest.raises(ValueError, match="Lab-only"):
        application._require_speech_model_ready("qwen3-base-clone-0.6b", surface="studio")


def test_current_qwen_1_7b_smoke_enables_lab_but_not_studio(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        model_pack_service_factory=lambda _registry, _models: _ReadyQwenPacks(
            "qwen3-tts-custom-voice-1.7b"
        ),
    )

    qwen = application.environment_status()["speech_models"]["qwen3-custom-voice-1.7b"]

    assert qwen["state"] == "ready"
    assert qwen["ready"] is True
    assert qwen["lab_only"] is True
    assert qwen["studio_eligible"] is False
    assert "calm" in qwen["capabilities"]["delivery"]["values"]
    application._require_speech_model_ready("qwen3-custom-voice-1.7b", surface="lab")
    with pytest.raises(ValueError, match="Lab-only"):
        application._require_speech_model_ready("qwen3-custom-voice-1.7b", surface="studio")


def test_lab_voice_promotion_requires_current_studio_eligibility() -> None:
    qwen_error = _studio_voice_promotion_error(
        {
            "speech_models": {
                "qwen3-custom-voice-0.6b": {
                    "studio_eligible": False,
                    "error": "no checksum-reviewed runtime",
                }
            }
        },
        "qwen3-custom-voice-0.6b",
    )
    kokoro_error = _studio_voice_promotion_error(
        {"speech_models": {"kokoro": {"studio_eligible": True, "error": None}}},
        "kokoro",
    )

    assert qwen_error is not None
    assert "not currently Studio-selectable" in qwen_error
    assert "checksum-reviewed runtime" in qwen_error
    assert kokoro_error is None
