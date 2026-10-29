from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import time
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from whoopy.evaluation import EvaluationRepository, ExperimentDefinition, PromotionDecision
from whoopy.model_packs.runtime import RuntimePackState, RuntimePackStatus
from whoopy.pipeline import RunStatus, RunStore
from whoopy.webui.api import (
    GenerationRequest,
    _candidate_product_value,
    _LabApiService,
    _promotion_setting_keys,
    _supported_low_resource_llm_ids,
    create_api,
)
from whoopy.webui.catalog import LocalCatalog
from whoopy.webui.server import LocalWebApplication


def _application(tmp_path: Path) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )


def test_model_role_defaults_do_not_overwrite_the_portable_default() -> None:
    def decision(role: str) -> PromotionDecision:
        return PromotionDecision.model_validate(
            {
                "schema_version": 2,
                "decision_id": str(uuid4()),
                "created_at": datetime.now(UTC).isoformat(),
                "experiment_id": "llm-review",
                "experiment_version": 1,
                "outcome": "promote",
                "studio_target": "llm",
                "selection_role": role,
                "candidate_id": role,
                "previous_value": "baseline",
                "selected_value": role,
                "rationale": "Blind evidence supports this role.",
                "report_sha256": "a" * 64,
                "rating_ids": [str(uuid4())],
            }
        )

    assert _promotion_setting_keys(decision("portable_default")) == (
        "studio.model_role.portable_default",
        "studio.default.llm",
    )
    assert _promotion_setting_keys(decision("low_resource_fallback")) == (
        "studio.model_role.low_resource_fallback",
    )
    assert _promotion_setting_keys(decision("higher_quality")) == (
        "studio.model_role.higher_quality",
    )


def test_low_resource_role_uses_only_production_lite_model_ids() -> None:
    assert _supported_low_resource_llm_ids() == ("qwen3-1.7b", "qwen3-1.7b-lite")
    assert "qwen3-4b-standard" not in _supported_low_resource_llm_ids()


def test_promoted_value_is_derived_from_typed_candidate_not_client_label() -> None:
    definition = ExperimentDefinition.model_validate(_unsupported_llm_experiment())
    candidate = definition.candidates[1].model_copy(update={"label": "Friendly but ambiguous"})

    assert _candidate_product_value("llm", candidate) == "unknown-0.6b"


def _voice_wav(*, seconds: int = 2, frequency: int = 220) -> bytes:
    rate = 24_000
    samples = array(
        "h",
        (
            round(5_000 * math.sin(2 * math.pi * frequency * index / rate))
            for index in range(rate * seconds)
        ),
    )
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples.tobytes())
    return output.getvalue()


VOICE_CONSENT = "I own this voice or have permission to clone it."


class _EmptyModelPacks:
    def list(self) -> dict[str, object]:
        return {"packs": []}


class _RuntimePacks:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.installs: list[tuple[str, bool]] = []

    def _status(self, runtime_id: str, state: RuntimePackState) -> RuntimePackStatus:
        return RuntimePackStatus(
            runtime_id=runtime_id,
            display_name="Test isolated runtime",
            state=state,
            revision="reviewed-1",
            target="darwin-arm64",
            directory=self.root / "managed/runtimes/test-runtime",
            message=(
                "Pinned runtime is available."
                if state is RuntimePackState.INSTALLED
                else "Pinned runtime is missing."
            ),
            remedy=None if state is RuntimePackState.INSTALLED else "Install it in Studio.",
        )

    def list(self) -> tuple[RuntimePackStatus, ...]:
        return (self._status("test-runtime", RuntimePackState.MISSING),)

    def get(self, _runtime_id: str) -> Any:
        class Bundle:
            target = "darwin-arm64"
            url = "https://example.test/runtime.tar.gz"

        class Spec:
            bundles = (Bundle(),)

        return Spec()

    def inspect(self, runtime_id: str) -> RuntimePackStatus:
        return self._status(runtime_id, RuntimePackState.MISSING)

    def install(
        self,
        runtime_id: str,
        *,
        offline_directory: Path | None,
        allow_network: bool,
    ) -> RuntimePackStatus:
        assert offline_directory is None
        self.installs.append((runtime_id, allow_network))
        return self._status(runtime_id, RuntimePackState.INSTALLED)


def test_fastapi_is_versioned_and_serves_the_bundled_product(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        health = client.get("/api/v1/health")
        page = client.get("/")
        asset_path = re.search(r'(?:src|href)="(/[^"]+\.js)"', page.text)
        assert asset_path is not None
        asset = client.get(asset_path.group(1))

    assert health.status_code == 200
    assert health.json()["api_version"] == 1
    assert health.json()["surfaces"] == ["studio", "lab"]
    assert page.status_code == 200
    assert "Whoopy Local Studio" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert asset.status_code == 200
    expected_cache = (
        "public, max-age=31536000, immutable"
        if "/_app/immutable/" in asset_path.group(1)
        else "no-store"
    )
    assert asset.headers["cache-control"] == expected_cache
    policy = page.headers["content-security-policy"]
    assert "script-src 'self' 'sha256-" in policy
    assert "script-src 'self' 'unsafe-inline'" not in policy


def test_served_studio_inline_bootstrap_is_authorized_by_csp(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        # Studio has one route; the retired /voices/ A/B workflow is no longer bundled.
        pages = (client.get("/"),)

    for page in pages:
        assert page.status_code == 200
        policy = page.headers["content-security-policy"]
        inline_scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", page.text, re.DOTALL)
        assert inline_scripts
        for source in inline_scripts:
            digest = base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
            assert f"'sha256-{digest}'" in policy


def test_runtime_install_and_verification_are_available_without_cli(tmp_path: Path) -> None:
    runtimes = _RuntimePacks(tmp_path)
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        runtime_pack_service_factory=lambda _path, _root: runtimes,
    )
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        listed = client.get("/api/v1/runtime-packs")
        verified = client.post("/api/v1/runtime-packs/test-runtime/verify", json={})
        started = client.post(
            "/api/v1/runtime-pack-tasks/test-runtime/install",
            json={"allow_network": True},
        )
        assert started.status_code == 202
        task_id = started.json()["task_id"]
        deadline = time.monotonic() + 2
        while True:
            task = client.get(f"/api/v1/runtime-pack-tasks/{task_id}")
            assert task.status_code == 200
            if task.json()["status"] != "running":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        wrong_namespace = client.get(f"/api/v1/model-pack-tasks/{task_id}")

    assert listed.status_code == 200
    assert listed.json()["runtime_packs"][0]["state"] == "missing"
    assert listed.json()["runtime_packs"][0]["browser_installable"] is True
    assert verified.status_code == 200
    assert task.json()["status"] == "completed"
    assert task.json()["resource_kind"] == "runtime"
    assert task.json()["result"]["state"] == "installed"
    assert runtimes.installs == [("test-runtime", True)]
    assert wrong_namespace.status_code == 404


def test_local_product_exposes_no_cloud_or_ambience_workflow(tmp_path: Path) -> None:
    """Keep explicitly deferred product surfaces out of the shipped local API."""

    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        paths = set(client.get("/api/openapi.json").json()["paths"])

    assert paths
    deferred_terms = ("ambience", "cloud", "account", "publish", "commons")
    assert not {path for path in paths if any(term in path.casefold() for term in deferred_terms)}


@pytest.mark.parametrize("audience", ["lab", "system"])
def test_nonstudio_meditation_has_durable_receipt_and_preserves_audience_on_restore(
    tmp_path: Path,
    audience: Literal["lab", "system"],
) -> None:
    application = _application(tmp_path)
    run_id = UUID("12345678-1234-5678-1234-567812345678")
    store = RunStore(application.runs_directory)
    queued = store.create(
        "A restorable meditation.",
        run_id=run_id,
        audience=audience,
    )
    assert queued.recovery is not None
    store.save(
        queued.transition(
            RunStatus.FAILED,
            updated_at=datetime.now(UTC),
            recovery=queued.recovery.model_copy(update={"process_attempts": 1}),
            error="Stopped before generation for the restore test.",
        )
    )

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        deleted = client.delete(f"/api/v1/runs/{run_id}")
        assert deleted.status_code == 200
        receipt_id = deleted.json()["receipt_id"]
        listed = client.get("/api/v1/trash/runs")
        assert listed.status_code == 200
        assert listed.json()["runs"] == [
            {
                "receipt_id": receipt_id,
                "run_id": str(run_id),
                "title": "A restorable meditation.",
                "audience": audience,
                "deleted_at": listed.json()["runs"][0]["deleted_at"],
                "restore_confirmation": f"restore meditation {receipt_id}",
            }
        ]
        refused = client.post(
            f"/api/v1/trash/runs/{receipt_id}/restore",
            params={"confirmation": "restore"},
        )
        restored = client.post(
            f"/api/v1/trash/runs/{receipt_id}/restore",
            params={"confirmation": f"restore meditation {receipt_id}"},
        )

    assert refused.status_code == 400
    assert restored.status_code == 200
    assert restored.json() == {
        "run_id": str(run_id),
        "receipt_id": receipt_id,
        "restored": True,
    }
    assert store.load(run_id).status is RunStatus.FAILED
    assert store.load(run_id).audience == audience
    assert application.list_deleted_runs() == []


def test_run_trash_refuses_tampered_receipts_and_symlinked_entries(tmp_path: Path) -> None:
    application = _application(tmp_path)
    run_id = UUID("22345678-1234-5678-1234-567812345678")
    store = RunStore(application.runs_directory)
    queued = store.create("Tamper-resistant trash.", run_id=run_id)
    assert queued.recovery is not None
    store.save(
        queued.transition(
            RunStatus.FAILED,
            updated_at=datetime.now(UTC),
            recovery=queued.recovery.model_copy(update={"process_attempts": 1}),
            error="Expected test failure.",
        )
    )
    deleted = application.delete_run(str(run_id))
    assert deleted is not None
    receipt_id = str(deleted["receipt_id"])
    entry = next((application.runs_directory / ".trash").iterdir())
    receipt = entry / "trash-receipt.json"
    receipt.write_text(receipt.read_text("utf-8").replace(str(run_id), str(UUID(int=0))), "utf-8")

    assert application.list_deleted_runs() == []
    assert (
        application.restore_deleted_run(
            receipt_id,
            confirmation=application.run_restore_confirmation(receipt_id),
        )
        is None
    )

    entry.replace(tmp_path / "tampered-entry")
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = (
        application.runs_directory
        / ".trash"
        / (f"20260805T120000-aaaaaaaaaa-{UUID('32345678-1234-5678-1234-567812345678')}")
    )
    symlink.symlink_to(outside, target_is_directory=True)
    assert application.list_deleted_runs() == []


def test_run_deletion_refuses_symlinked_trash_root(tmp_path: Path) -> None:
    application = _application(tmp_path)
    application.runs_directory.mkdir(parents=True)
    outside = tmp_path / "outside-trash"
    outside.mkdir()
    (application.runs_directory / ".trash").symlink_to(outside, target_is_directory=True)
    run_id = UUID("42345678-1234-5678-1234-567812345678")
    store = RunStore(application.runs_directory)
    queued = store.create("Never leave the run root.", run_id=run_id)
    assert queued.recovery is not None
    store.save(
        queued.transition(
            RunStatus.FAILED,
            updated_at=datetime.now(UTC),
            recovery=queued.recovery.model_copy(update={"process_attempts": 1}),
            error="Expected test failure.",
        )
    )

    with pytest.raises(ValueError, match="must not be symbolic links"):
        application.delete_run(str(run_id))

    assert store.load(run_id).status is RunStatus.FAILED
    assert list(outside.iterdir()) == []


def test_speech_model_contract_exposes_profiles_and_only_typed_request_fields(
    tmp_path: Path,
) -> None:
    """Keep capability-driven clients aligned with the versioned request schema."""

    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        model_pack_service_factory=lambda _registry, _models: _EmptyModelPacks(),
    )
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/speech-models")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["profiles"]) == {"basic", "lite", "standard", "high"}
    for profile in payload["profiles"].values():
        assert set(profile) == {"compatible", "installed", "messages", "artifact_count"}

    request_properties = set(GenerationRequest.model_json_schema()["properties"])
    models = payload["speech_models"]
    assert models
    for model in models.values():
        capabilities = model.get("capabilities")
        if capabilities is None:
            continue
        assert set(capabilities["request_fields"]) <= request_properties
        assert capabilities["voice_modes"]
        assert capabilities["delivery"]["values"]
        assert capabilities["intensity"]["values"]
        minimum, maximum = capabilities["pace"]["speed_range"]
        assert 0.5 <= minimum <= maximum <= 1.2
        assert capabilities["pace"]["values"]
        assert "pace" in capabilities["request_fields"]

    assert models["kokoro"]["capabilities"]["delivery"] == {
        "values": ["neutral"],
        "fallback": "neutral",
    }
    assert models["kokoro"]["capabilities"]["intensity"] == {
        "values": ["subtle"],
        "fallback": "subtle",
    }
    assert models["moss-local-v1.5"]["capabilities"]["intensity"]["values"] == [
        "subtle",
        "moderate",
    ]
    assert "intensity" in models["moss-local-v1.5"]["capabilities"]["request_fields"]
    assert "Qwen3-4B language backbone" in models["moss-local-v1.5"]["architecture_note"]

    assert models["kokoro"]["studio_candidate"] is True
    assert models["kokoro"]["studio_eligible"] is False
    assert models["kokoro"]["lab_only"] is False
    assert models["kokoro"]["machine_resolution"] == "unresolved"
    assert "Install kokoro" in models["kokoro"]["error"]
    assert models["kokoro"]["readiness"] == {
        "installed_files": {
            "passed": False,
            "message": "Pinned model files have not been verified.",
        },
        "runtime_probe": {
            "passed": False,
            "message": "No current-machine runtime probe has passed.",
        },
        "playable_synthesis": {
            "passed": False,
            "message": "No playable synthesis matches this model and runtime revision.",
        },
    }
    assert models["qwen3-custom-voice-0.6b"]["studio_eligible"] is False
    assert models["qwen3-custom-voice-0.6b"]["lab_only"] is True
    assert models["moss-local-v1.5"]["capabilities"]["supports_pronunciation_overrides"] is True
    assert "supports_pronunciation_overrides" not in models["kokoro"]["capabilities"]


def test_generation_request_accepts_only_canonical_global_pace_intents() -> None:
    request = GenerationRequest(mode="script", text="Rest here.", pace="very_slow")

    assert request.pace == "very_slow"
    with pytest.raises(ValueError, match="pace"):
        GenerationRequest.model_validate({"mode": "script", "text": "Rest here.", "pace": "0.6"})


def test_status_does_not_call_json_only_smoke_evidence_playable(tmp_path: Path) -> None:
    class JsonOnlySmokePacks:
        def list(self) -> dict[str, object]:
            return {
                "packs": [
                    {
                        "pack_id": "kokoro",
                        "state": "installed",
                        "checks": [
                            {
                                "check": "offline_smoke_test",
                                "passed": True,
                                "message": "historical JSON says synthesis passed",
                            },
                            {
                                "check": "playable_smoke_sample",
                                "passed": False,
                                "message": "retained sample is missing",
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
        model_pack_service_factory=lambda _registry, _models: JsonOnlySmokePacks(),
    )

    kokoro = application.environment_status()["speech_models"]["kokoro"]

    assert kokoro["ready"] is False
    assert kokoro["studio_eligible"] is False
    assert kokoro["readiness"]["playable_synthesis"]["passed"] is False
    assert "retained sample is missing" in kokoro["readiness"]["playable_synthesis"]["message"]


def test_environment_status_reuses_short_lived_verified_snapshot(tmp_path: Path) -> None:
    class CountingPacks:
        def __init__(self) -> None:
            self.calls = 0

        def list(self) -> dict[str, object]:
            self.calls += 1
            return {"packs": []}

    packs = CountingPacks()
    project_root = Path(__file__).resolve().parents[1]
    application = LocalWebApplication(
        project_root=project_root,
        config_directory=project_root / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        model_pack_service_factory=lambda _registry, _models: packs,
    )

    first = application.environment_status()
    second = application.environment_status()

    assert first == second
    assert first is not second
    assert packs.calls == 1

    application._invalidate_environment_status()
    application.environment_status()
    assert packs.calls == 2


def test_revision_api_accepts_only_typed_pronunciation_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    captured: dict[str, object] = {}

    def edit_run(run_id: str, payload: object) -> dict[str, object]:
        captured["run_id"] = run_id
        captured["payload"] = payload
        return {"revision": {"revision_id": "r0002-pronunciation"}}

    monkeypatch.setattr(application, "edit_run", edit_run)
    run_id = "99999999-9999-4999-8999-999999999999"
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.post(
            f"/api/v1/runs/{run_id}/revisions",
            json={
                "timeline": {
                    "pronunciation_overrides": {
                        "speech-0001": [{"source": "Whoopy", "pronunciation": "Woo-pee"}]
                    }
                }
            },
        )

    assert response.status_code == 202
    assert captured["run_id"] == run_id
    payload = captured["payload"]
    assert isinstance(payload, dict)
    timeline = payload["timeline"]
    assert isinstance(timeline, dict)
    assert timeline["pronunciation_overrides"]["speech-0001"] == [
        {"source": "Whoopy", "pronunciation": "Woo-pee"}
    ]


def test_fastapi_rejects_cross_origin_mutation(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/v1/model-packs/unload",
            json={},
            headers={"Origin": "https://example.invalid"},
        )

    assert response.status_code == 403
    assert "local Whoopy page" in response.json()["error"]


def test_fastapi_rejects_dns_rebinding_host_header(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/health", headers={"Host": "attacker.example"})

    assert response.status_code == 400


def test_model_pack_smoke_sample_is_returned_as_playable_uncached_wave(tmp_path: Path) -> None:
    sample = tmp_path / "sample.wav"
    sample.write_bytes(_voice_wav(seconds=1))

    class SamplePacks:
        def list(self) -> dict[str, object]:
            return {"packs": [{"pack_id": "moss-local-5b", "state": "ready"}]}

        def smoke_sample_path(self, pack_id: str) -> Path:
            assert pack_id == "moss-local-5b"
            return sample

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        model_pack_service_factory=lambda _registry, _models: SamplePacks(),
    )
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/model-packs/moss-local-5b/sample")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    with wave.open(io.BytesIO(response.content), "rb") as source:
        assert source.getnframes() == 24_000


def test_long_model_smoke_is_background_and_pollable_with_live_metrics(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowPacks:
        def list(self) -> dict[str, object]:
            return {
                "packs": [
                    {
                        "pack_id": "kokoro",
                        "state": "ready",
                        "machine_resolution": "playable",
                    }
                ]
            }

        def smoke_test(self, pack_id: str) -> dict[str, object]:
            assert pack_id == "moss-local-5b"
            entered.set()
            assert release.wait(timeout=2)
            return {"pack_id": pack_id, "state": "ready"}

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        model_pack_service_factory=lambda _registry, _models: SlowPacks(),
    )
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        started = client.post(
            "/api/v1/model-pack-tasks/moss-local-5b/smoke-test",
            json={},
        )
        assert started.status_code == 202
        task_id = started.json()["task_id"]
        assert entered.wait(timeout=2)

        running = client.get(f"/api/v1/model-pack-tasks/{task_id}")
        assert running.status_code == 200
        assert running.json()["status"] == "running"
        assert running.json()["stage"] == "runtime_probe_and_synthesis"
        assert running.json()["elapsed_seconds"] >= 0
        assert running.json()["resident_memory_bytes"] > 0
        assert running.json()["cancellable"] is False

        with pytest.raises(ValueError, match="active model or runtime operation"):
            application.start_generation(
                {"mode": "prompt", "text": "Do not overlap model loading.", "minutes": 1}
            )
        competing = client.post(
            "/api/v1/model-packs/moss-local-5b/verify",
            json={},
        )
        assert competing.status_code == 400
        assert "active model or runtime operation" in competing.json()["error"]

        release.set()
        deadline = time.monotonic() + 2
        while True:
            completed = client.get(f"/api/v1/model-pack-tasks/{task_id}").json()
            if completed["status"] == "completed":
                break
            if time.monotonic() >= deadline:
                pytest.fail("background model smoke did not finish")
            time.sleep(0.01)

    assert completed["result"]["state"] == "ready"
    assert "playable synthesis" in completed["message"].lower()


def test_catalog_is_rebuildable_and_searches_run_metadata(tmp_path: Path) -> None:
    catalog = LocalCatalog(tmp_path / "catalog.sqlite3")
    catalog.replace_runs(
        [
            {
                "run_id": "run-1",
                "title": "Evening body scan",
                "status": "completed",
                "source_kind": "generated_prompt",
                "tts_model": "kokoro",
                "voice": "af_heart",
                "duration_seconds": 300.0,
                "quality_passed": True,
                "has_audio": True,
                "created_at": "2026-08-01T00:00:00+00:00",
                "updated_at": "2026-08-01T00:05:00+00:00",
            }
        ]
    )

    assert catalog.search_runs(query="body", tts_model="kokoro")[0]["title"] == (
        "Evening body scan"
    )
    assert catalog.search_runs(query="missing") == []


def test_catalog_rebuilds_versioned_lab_projection_atomically(tmp_path: Path) -> None:
    catalog = LocalCatalog(tmp_path / "catalog.sqlite3")
    definition = {"schema_version": 1, "experiment_type": "voice"}
    catalog.replace_lab_projection(
        experiments=[
            {
                "experiment_id": "voice-review",
                "experiment_version": version,
                "kind": "voice",
                "status": "complete" if version == 1 else "defined",
                "definition": {**definition, "version": version},
                "definition_path": f"/lab/voice-review/v{version}/definition.json",
                "report_path": ("/lab/voice-review/v1/report.json" if version == 1 else None),
                "updated_at": "2026-08-05T12:00:00+00:00",
            }
            for version in (1, 2)
        ],
        ratings=[
            {
                "rating_id": "rating-1",
                "experiment_id": "voice-review",
                "experiment_version": 1,
                "assignment_id": "assignment-1",
                "dimension": "voice",
                "score": 5,
                "preferred_side": "right",
                "notes": "Warmer delivery.",
                "created_at": "2026-08-05T12:01:00+00:00",
            }
        ],
        decisions=[
            {
                "decision_id": "decision-1",
                "experiment_id": "voice-review",
                "experiment_version": 1,
                "outcome": "promote",
                "target": "voice",
                "selection_role": None,
                "candidate_id": "warm",
                "selected_value": "warm",
                "rationale": "Blind review preferred it.",
                "created_at": "2026-08-05T12:02:00+00:00",
            }
        ],
    )

    projection = catalog.lab_projection()

    assert [
        (value["experiment_id"], value["experiment_version"]) for value in projection["experiments"]
    ] == [("voice-review", 1), ("voice-review", 2)]
    assert projection["ratings"][0]["experiment_version"] == 1
    assert projection["decisions"][0]["experiment_version"] == 1
    assert projection["decisions"][0]["selection_role"] is None

    catalog.replace_lab_projection(experiments=[], ratings=[], decisions=[])
    assert catalog.lab_projection() == {"experiments": [], "ratings": [], "decisions": []}


def test_catalog_migrates_old_lab_tables_without_losing_settings(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('schema_version', '2');
            CREATE TABLE settings (
                key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO settings VALUES ('studio.minutes', '5', '2026-08-05T00:00:00Z');
            CREATE TABLE experiments (
                experiment_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                definition_json TEXT NOT NULL, report_path TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE ratings (
                rating_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL, dimension TEXT NOT NULL, score INTEGER NOT NULL,
                notes TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE decisions (
                decision_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                target TEXT NOT NULL, value_json TEXT NOT NULL, rationale TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    catalog = LocalCatalog(path)

    assert catalog.export_settings() == {"studio.minutes": 5}
    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        columns = {
            row[1]: row[5]
            for row in connection.execute("PRAGMA table_info(experiments)").fetchall()
        }
    assert version == "4"
    assert columns["experiment_id"] == 1
    assert columns["experiment_version"] == 2
    assert catalog.lab_projection()["experiments"] == []


def test_catalog_and_runs_api_keep_lab_artifacts_out_of_studio_library(tmp_path: Path) -> None:
    application = _application(tmp_path)
    store = RunStore(tmp_path / "runs")
    studio = store.create("A personal grounding meditation.")
    lab = store.create("Lab comparison artifact.", audience="lab")
    system = store.create("Acceptance diagnostic artifact.", audience="system")

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        studio_response = client.get("/api/v1/runs")
        lab_response = client.get("/api/v1/runs?audience=lab")
        system_response = client.get("/api/v1/runs?audience=system")
        all_response = client.get("/api/v1/runs?audience=all")
        direct_lab_response = client.get(f"/api/v1/runs/{lab.run_id}")
        direct_system_response = client.get(f"/api/v1/runs/{system.run_id}")

    assert [run["run_id"] for run in studio_response.json()["runs"]] == [str(studio.run_id)]
    assert [run["run_id"] for run in lab_response.json()["runs"]] == [str(lab.run_id)]
    assert [run["run_id"] for run in system_response.json()["runs"]] == [str(system.run_id)]
    assert {run["run_id"] for run in all_response.json()["runs"]} == {
        str(studio.run_id),
        str(lab.run_id),
        str(system.run_id),
    }
    assert direct_lab_response.json()["audience"] == "lab"
    assert direct_system_response.json()["audience"] == "system"


def test_catalog_migrates_pre_audience_run_projection(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                source_kind TEXT NOT NULL, tts_model TEXT, voice TEXT,
                duration_seconds REAL, quality_passed INTEGER, has_audio INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, search_text TEXT NOT NULL
            );
            """
        )

    catalog = LocalCatalog(path)
    with sqlite3.connect(path) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)").fetchall()}

    assert "audience" in columns
    assert catalog.search_runs() == []


def test_catalog_filters_runs_by_model_duration_and_creation_date(tmp_path: Path) -> None:
    catalog = LocalCatalog(tmp_path / "catalog.sqlite3")
    catalog.replace_runs(
        [
            {
                "run_id": "short",
                "title": "Short grounding",
                "status": "completed",
                "source_kind": "generated_prompt",
                "tts_model": "kokoro",
                "voice": "af_heart",
                "duration_seconds": 180.0,
                "quality_passed": True,
                "has_audio": True,
                "created_at": "2026-07-01T10:00:00+00:00",
                "updated_at": "2026-07-01T10:05:00+00:00",
            },
            {
                "run_id": "long",
                "title": "Long body scan",
                "status": "completed",
                "source_kind": "generated_prompt",
                "tts_model": "fish-1.4",
                "voice": "reference-1",
                "duration_seconds": 1_200.0,
                "quality_passed": True,
                "has_audio": True,
                "created_at": "2026-08-02T10:00:00+00:00",
                "updated_at": "2026-08-02T10:20:00+00:00",
            },
        ]
    )

    selected = catalog.search_runs(
        tts_model="fish-1.4",
        minimum_duration_seconds=600,
        maximum_duration_seconds=1_800,
        created_from=datetime(2026, 8, 1, tzinfo=UTC),
        created_to=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert [run["run_id"] for run in selected] == ["long"]


def test_runs_api_rejects_inverted_library_ranges(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        duration = client.get(
            "/api/v1/runs?minimum_duration_seconds=600&maximum_duration_seconds=300"
        )
        created = client.get(
            "/api/v1/runs?created_from=2026-08-03T00:00:00Z&created_to=2026-08-01T00:00:00Z"
        )

    assert duration.status_code == 400
    assert duration.json()["error"] == "Minimum duration cannot exceed maximum duration."
    assert created.status_code == 400
    assert created.json()["error"] == "Creation start cannot be after creation end."


def test_settings_round_trip_through_versioned_api(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        imported = client.post(
            "/api/v1/settings/import",
            json={
                "schema_version": 1,
                "settings": {"studio.default_delivery": "calm", "studio.minutes": 5},
            },
        )
        exported = client.get("/api/v1/settings/export")

    assert imported.status_code == 200
    assert exported.json()["settings"] == {
        "studio.default_delivery": "calm",
        "studio.minutes": 5,
    }
    assert exported.json()["local_data"]["runs"].endswith("runs")
    assert exported.json()["local_data"]["lab_evidence"].endswith("runs/.whoopy-lab")


def test_reviewed_pacing_profiles_are_exposed_with_content_identity(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/pacing-profiles")

    assert response.status_code == 200
    profiles = {item["profile_id"]: item for item in response.json()["profiles"]}
    assert profiles["meditative-v2"]["silence_strategy"] == "preserve"
    assert profiles["progressive-quiet-v2"]["silence_strategy"] == "progressive_quiet"
    assert len(profiles["progressive-quiet-v2"]["sha256"]) == 64


def test_catalog_upserts_one_promoted_default_without_erasing_preferences(tmp_path: Path) -> None:
    catalog = LocalCatalog(tmp_path / "catalog.sqlite3")
    catalog.import_settings({"studio.minutes": 5})

    catalog.set_setting(
        "studio.default.voice",
        {"value": "candidate-b", "decision_id": "decision-1"},
    )

    assert catalog.export_settings() == {
        "studio.default.voice": {
            "value": "candidate-b",
            "decision_id": "decision-1",
        },
        "studio.minutes": 5,
    }


def test_voice_reference_full_versioned_api_workflow(tmp_path: Path) -> None:
    application = _application(tmp_path)
    parameters = {
        "reference_id": "my-narrator",
        "display_name": "My narrator",
        "transcript": "This is the exact reference transcript.",
        "source_kind": "user_provided",
    }
    headers = {
        "Content-Type": "audio/wav",
        "X-Whoopy-Voice-Consent": VOICE_CONSENT,
    }
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        imported = client.post(
            "/api/v1/voice-references",
            params=parameters,
            headers=headers,
            content=_voice_wav(),
        )
        listed = client.get("/api/v1/voice-references")
        metadata = client.get("/api/v1/voice-references/my-narrator")
        preview = client.get("/api/v1/voice-references/my-narrator/preview")
        replaced = client.put(
            "/api/v1/voice-references/my-narrator",
            params={
                "display_name": "My narrator v2",
                "transcript": "This is the replacement transcript.",
                "source_kind": "user_provided",
                "trim_start_ms": 500,
                "trim_end_ms": 1_500,
            },
            headers=headers,
            content=_voice_wav(frequency=330),
        )
        confirmation = application.voice_reference_manager.delete_confirmation("my-narrator")
        deleted = client.delete(
            "/api/v1/voice-references/my-narrator",
            params={"confirmation": confirmation},
        )
        assert client.get("/api/v1/voice-references").json() == {"voice_references": []}
        trash_id = deleted.json()["trash_id"]
        restored = client.post(
            f"/api/v1/voice-references/trash/{trash_id}/restore",
            params={
                "confirmation": application.voice_reference_manager.restore_confirmation(trash_id)
            },
        )

    assert imported.status_code == 201
    assert imported.json()["revision"] == 1
    assert imported.json()["consent"]["confirmed"] is True
    assert listed.json()["voice_references"][0]["reference_id"] == "my-narrator"
    assert metadata.json()["audio_path"].startswith("references/")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("audio/wav")
    assert preview.headers["x-whoopy-preview-duration-ms"] == "2000"
    assert preview.content.startswith(b"RIFF")
    assert replaced.status_code == 201
    assert replaced.json()["revision"] == 2
    assert replaced.json()["parent_revision_id"] == imported.json()["revision_id"]
    assert deleted.status_code == 200
    assert restored.status_code == 200
    assert restored.json()["revision"] == 2


def test_project_demo_reference_is_verified_read_only_and_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo_wav = tmp_path / "demo.wav"
    demo_wav.write_bytes(_voice_wav())

    class DemoReference:
        reference_id = "whoopy-local-reference-v1"
        audio_path = demo_wav
        consent_scope = "local_voice_cloning_experiment_only"

    monkeypatch.setattr(
        "whoopy.webui.server.resolve_voice_reference",
        lambda *_args, **_kwargs: DemoReference(),
    )
    application = _application(tmp_path)
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        metadata = client.get("/api/v1/voice-demo-reference")
        preview = client.get("/api/v1/voice-demo-reference/preview")
        managed = client.get("/api/v1/voice-references")

    assert metadata.status_code == 200
    assert metadata.json() == {
        "reference_id": "whoopy-local-reference-v1",
        "display_name": "Whoopy local demo reference",
        "duration_ms": 2000,
        "source_kind": "project_generated",
        "consent_scope": "local_voice_cloning_experiment_only",
        "read_only": True,
    }
    assert preview.status_code == 200
    assert preview.headers["x-whoopy-preview-duration-ms"] == "2000"
    assert preview.content.startswith(b"RIFF")
    assert managed.json() == {"voice_references": []}


def test_voice_reference_api_requires_exact_consent_and_wav_content_type(tmp_path: Path) -> None:
    parameters = {
        "reference_id": "blocked",
        "display_name": "Blocked",
        "transcript": "Transcript.",
        "source_kind": "user_provided",
    }
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        wrong_content_type = client.post(
            "/api/v1/voice-references",
            params=parameters,
            headers={
                "Content-Type": "application/json",
                "X-Whoopy-Voice-Consent": VOICE_CONSENT,
            },
            content=_voice_wav(),
        )
        wrong_consent = client.post(
            "/api/v1/voice-references",
            params=parameters,
            headers={
                "Content-Type": "audio/wav",
                "X-Whoopy-Voice-Consent": "yes",
            },
            content=_voice_wav(),
        )

    assert wrong_content_type.status_code == 415
    assert wrong_consent.status_code == 400
    assert "must exactly state" in wrong_consent.json()["error"]


def test_voice_reference_api_keeps_normal_json_body_limit(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/v1/generate",
            headers={"Content-Type": "application/json", "Content-Length": "100000"},
            content=b"{}",
        )

    assert response.status_code == 413


def test_fastapi_enforces_actual_chunked_json_body_size_without_content_length(
    tmp_path: Path,
) -> None:
    chunks = iter((b"{" + b"x" * 39_999, b"x" * 40_000 + b"}"))
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/v1/generate",
            headers={"Content-Type": "application/json"},
            content=chunks,
        )

    assert response.status_code == 413


def test_non_upload_voice_routes_do_not_receive_the_large_wav_body_allowance(
    tmp_path: Path,
) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.request(
            "DELETE",
            "/api/v1/voice-references/example",
            headers={"Content-Type": "application/json", "Content-Length": "100000"},
            content=b"{}",
        )

    assert response.status_code == 413


def test_generation_contract_rejects_cross_model_controls_and_lab_only_studio_use() -> None:
    with pytest.raises(ValueError, match="MOSS controls are valid only"):
        GenerationRequest.model_validate(
            {"mode": "script", "text": "Welcome.", "moss_language": "English"}
        )
    request = GenerationRequest.model_validate(
        {
            "mode": "script",
            "text": "Welcome.",
            "tts_model": "qwen3-custom-voice-0.6b",
            "qwen_speaker": "Serena",
            "delivery": "neutral",
            "pace": "natural",
        }
    )
    assert request.surface == "studio"
    request = GenerationRequest.model_validate(
        {
            "surface": "lab",
            "mode": "script",
            "text": "Welcome.",
            "tts_model": "qwen3-custom-voice-0.6b",
            "qwen_language": "English",
            "qwen_speaker": "Serena",
            "delivery": "neutral",
            "pace": "natural",
        }
    )
    assert request.surface == "lab"
    assert request.qwen_speaker == "Serena"
    with pytest.raises(ValueError, match="Custom generation seeds are Lab-only"):
        GenerationRequest.model_validate({"mode": "prompt", "text": "Welcome.", "seed": 314159})
    with pytest.raises(ValueError, match="Free-form MOSS instructions are Lab-only"):
        GenerationRequest.model_validate(
            {
                "mode": "script",
                "text": "Welcome.",
                "tts_model": "moss-local-v1.5",
                "moss_instruction": "Imitate a particular commercial narrator.",
            }
        )
    lab_moss = GenerationRequest.model_validate(
        {
            "surface": "lab",
            "mode": "script",
            "text": "Welcome.",
            "tts_model": "moss-local-v1.5",
            "moss_instruction": "Use a restrained, experimental delivery.",
            "seed": 314159,
        }
    )
    assert lab_moss.seed == 314159
    assert lab_moss.moss_instruction == "Use a restrained, experimental delivery."
    fish = GenerationRequest.model_validate(
        {
            "mode": "script",
            "text": "Welcome.",
            "tts_model": "fish-1.4",
            "voice_reference": "my-narrator",
        }
    )
    assert fish.voice_reference == "my-narrator"
    with pytest.raises(ValueError, match="direct voice mode"):
        GenerationRequest.model_validate(
            {
                "mode": "script",
                "text": "Welcome.",
                "tts_model": "moss-local-v1.5",
                "moss_use_reference": False,
                "voice_reference": "my-narrator",
            }
        )


def _unsupported_llm_experiment() -> dict[str, object]:
    base_condition = {
        "writing_method": "blueprint_first_sparse",
        "voice": {
            "configuration_id": "kokoro-heart-calm",
            "model_id": "kokoro",
            "voice_id": "af_heart",
            "voice_mode": "preset",
            "delivery": "calm",
            "intensity": "subtle",
            "language": "English",
        },
        "pacing_profile_id": "meditative-v2",
    }
    return {
        "schema_version": 1,
        "experiment_id": "api-unsupported-llms",
        "version": 1,
        "experiment_type": "llm",
        "evaluation_id": "local-v1-meditation-corpus",
        "evaluation_version": 1,
        "seed": 42,
        "baseline_candidate_id": "lite",
        "candidates": [
            {
                "candidate_id": "lite",
                "label": "Unknown 0.8B",
                "condition": {**base_condition, "llm_model_id": "unknown-0.8b"},
            },
            {
                "candidate_id": "tiny",
                "label": "Unknown 0.6B",
                "condition": {**base_condition, "llm_model_id": "unknown-0.6b"},
            },
        ],
    }


def _unsupported_writing_method_experiment() -> dict[str, object]:
    definition = _unsupported_llm_experiment()
    definition.update(
        {
            "experiment_id": "api-unrouted-writing-methods",
            "experiment_type": "writing_method",
            "baseline_candidate_id": "blueprint",
        }
    )
    candidates = definition["candidates"]
    assert isinstance(candidates, list)
    candidates[0] = {
        "candidate_id": "blueprint",
        "label": "Blueprint-first sparse",
        "condition": {
            **candidates[0]["condition"],
            "llm_model_id": "qwen3-4b",
            "writing_method": "blueprint_first_sparse",
        },
    }
    candidates[1] = {
        "candidate_id": "selection",
        "label": "Three candidate selection",
        "condition": {
            **candidates[1]["condition"],
            "llm_model_id": "qwen3-4b",
            "writing_method": "three_candidate_structural_selection",
        },
    }
    return definition


def _delivery_experiment(*, change_voice_identity: bool = False) -> dict[str, object]:
    base = _unsupported_llm_experiment()
    base.update(
        {
            "experiment_id": "api-delivery-presets",
            "experiment_type": "delivery",
            "baseline_candidate_id": "calm",
        }
    )
    candidates = base["candidates"]
    assert isinstance(candidates, list)
    fixed_voice = {
        "configuration_id": "qwen-serena",
        "model_id": "qwen3-custom-voice-1.7b",
        "voice_id": "Serena",
        "voice_mode": "preset",
        "language": "English",
    }
    candidates[0] = {
        "candidate_id": "calm",
        "label": "calm|subtle",
        "condition": {
            **candidates[0]["condition"],
            "llm_model_id": "qwen3-4b",
            "voice": {**fixed_voice, "delivery": "calm", "intensity": "subtle"},
        },
    }
    candidates[1] = {
        "candidate_id": "sleepy",
        "label": "sleepy|moderate",
        "condition": {
            **candidates[1]["condition"],
            "llm_model_id": "qwen3-4b",
            "voice": {
                **fixed_voice,
                "voice_id": "Vivian" if change_voice_identity else "Serena",
                "delivery": "sleepy",
                "intensity": "moderate",
            },
        },
    }
    return base


def test_api_startup_rebuilds_all_versioned_lab_definitions_from_json(tmp_path: Path) -> None:
    application = _application(tmp_path)
    repository = EvaluationRepository(tmp_path / "runs" / ".whoopy-lab")
    for version in (1, 2):
        payload = _unsupported_llm_experiment()
        payload["version"] = version
        repository.save_definition(ExperimentDefinition.model_validate(payload))

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/lab/experiments")

    assert response.status_code == 200
    assert [item["definition"]["version"] for item in response.json()["experiments"]] == [1, 2]
    projection = LocalCatalog(tmp_path / "runs" / ".whoopy-catalog.sqlite3").lab_projection()
    assert [
        (item["experiment_id"], item["experiment_version"]) for item in projection["experiments"]
    ] == [("api-unsupported-llms", 1), ("api-unsupported-llms", 2)]
    assert all(item["status"] == "defined" for item in projection["experiments"])


@pytest.mark.parametrize("missing_active_run", [False, True])
def test_lab_restart_pauses_stale_batch_until_explicit_resume(
    tmp_path: Path, missing_active_run: bool
) -> None:
    evaluation_directory = tmp_path / "config" / "evaluation"
    evaluation_directory.mkdir(parents=True)
    evaluation_directory.joinpath("local-v1.yaml").write_text(
        Path("config/evaluation/local-v1.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    application = _application(tmp_path)
    repository = EvaluationRepository(application.runs_directory / ".whoopy-lab")
    definition = ExperimentDefinition.model_validate(_unsupported_llm_experiment())
    repository.save_definition(definition)
    active_run_id = uuid4() if missing_active_run else None
    repository.save_execution_control(
        experiment_id=definition.experiment_id,
        version=definition.version,
        payload={
            "schema_version": 1,
            "experiment_id": definition.experiment_id,
            "experiment_version": definition.version,
            "status": "running",
            "requested_case_ids": ["grounding-3m"],
            "trials": [],
            "resumable_runs": {},
            "active_run_id": str(active_run_id) if active_run_id is not None else None,
            "halt_action": None,
            "failure": None,
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        before = client.get("/api/v1/lab/experiments").json()["experiments"][0]
        assert before["execution"]["status"] == "paused"
        assert before["queue"] is None
        assert application.list_runs() == []

        resumed = client.post(
            "/api/v1/lab/experiments/api-unsupported-llms/versions/1/resume",
            json={},
        )
        assert resumed.status_code == 202
        deadline = time.monotonic() + 5
        state = "running"
        while time.monotonic() < deadline:
            experiment = client.get("/api/v1/lab/experiments").json()["experiments"][0]
            state = experiment["execution"]["status"]
            if state == "completed":
                break
            time.sleep(0.01)

    assert state == "completed"
    report = repository.load_report(experiment_id=definition.experiment_id, version=1)
    assert [trial.candidate_id for trial in report.trials] == ["lite", "tiny"]
    assert all(not trial.success for trial in report.trials)
    runs = application.list_runs()
    if active_run_id is not None:
        # A lost owned run is retained as failed evidence, not silently regenerated.
        assert report.trials[0].source_run_id == active_run_id
        assert report.trials[0].failure == "Interrupted production run no longer exists."
        assert len(runs) == 1
    else:
        assert len(runs) == 2
        assert report.trials[0].failure == (
            "LLM 'unknown-0.8b' is not exposed by the production generator."
        )
    assert report.trials[1].failure == (
        "LLM 'unknown-0.6b' is not exposed by the production generator."
    )
    assert str(report.trials[1].source_run_id) in {run["run_id"] for run in runs}


def test_lab_resume_reconciles_pause_before_first_trial_queue_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal queue attempt and resumable control must not wedge one revision."""

    application = _application(tmp_path)
    definition = ExperimentDefinition.model_validate(_unsupported_llm_experiment())
    service = _LabApiService(application=application, executor=cast(Any, object()))
    service.add_definition(definition)
    service.queue.enqueue(definition)
    assert service.queue.start_next() is not None
    service.queue.finish(
        experiment_id=definition.experiment_id,
        experiment_version=definition.version,
        failure="batch paused",
    )
    key = (definition.experiment_id, definition.version)
    control = service._new_control(definition, ("grounding-3m",))
    # This is the exact persisted divergence produced when resume raced the
    # pause worker's terminal queue update.
    control["status"] = "queued"
    service._save_control(key, control)
    monkeypatch.setattr(service, "_drain_queue", lambda _definition: None)

    resumed = service.execute(
        experiment_id=definition.experiment_id,
        version=definition.version,
        case_ids=["grounding-3m"],
    )

    assert resumed["accepted"] is True
    assert resumed["status"] == "running"
    entries = service.queue.entries()
    assert [entry.status for entry in entries] == ["failed", "running"]
    assert service._controls[key]["status"] == "queued"


def test_lab_capabilities_expose_production_writing_methods_and_accept_queue(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        capabilities = client.get("/api/v1/lab/capabilities")
        accepted = client.post(
            "/api/v1/lab/experiments",
            json=_unsupported_writing_method_experiment(),
        )
        experiments = client.get("/api/v1/lab/experiments")

    assert capabilities.status_code == 200
    payload = capabilities.json()
    methods = {method["id"]: method for method in payload["writing_methods"]}
    assert methods["blueprint_first_sparse"]["production_backed"] is True
    assert methods["blueprint_first_sparse"]["available"] is True
    assert methods["plan_first"]["available"] is True
    assert methods["three_candidate_structural_selection"]["available"] is True
    assert payload["experiment_types"]["writing_method"]["available"] is True
    assert accepted.status_code == 201
    assert len(experiments.json()["experiments"]) == 1


def test_lab_api_exposes_delivery_axis_and_rejects_mixed_voice_identity(
    tmp_path: Path,
) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        capabilities = client.get("/api/v1/lab/capabilities")
        accepted = client.post("/api/v1/lab/experiments", json=_delivery_experiment())
        rejected = client.post(
            "/api/v1/lab/experiments",
            json=_delivery_experiment(change_voice_identity=True),
        )

    assert capabilities.json()["experiment_types"]["delivery"]["available"] is True
    assert accepted.status_code == 201
    assert rejected.status_code == 422
    assert "may only change voice.delivery" in rejected.text


def test_lab_api_runs_the_production_contract_and_persists_honest_failures(
    tmp_path: Path,
) -> None:
    evaluation_directory = tmp_path / "config" / "evaluation"
    evaluation_directory.mkdir(parents=True)
    evaluation_directory.joinpath("local-v1.yaml").write_text(
        Path("config/evaluation/local-v1.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    application = _application(tmp_path)

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        sets = client.get("/api/v1/lab/evaluation-sets")
        acceptance = client.get(
            "/api/v1/lab/evaluation-sets/local-v1-meditation-corpus/versions/1/acceptance"
        )
        decisions = client.get("/api/v1/lab/decisions")
        created = client.post("/api/v1/lab/experiments", json=_unsupported_llm_experiment())
        started = client.post(
            "/api/v1/lab/experiments/api-unsupported-llms/versions/1/execute",
            json={"case_ids": ["grounding-3m"]},
        )
        deadline = time.monotonic() + 5
        state = "queued"
        while time.monotonic() < deadline:
            experiments = client.get("/api/v1/lab/experiments").json()["experiments"]
            state = experiments[0]["queue"]["status"]
            if state in {"completed", "failed"}:
                break
            time.sleep(0.01)
        report = client.get("/api/v1/lab/experiments/api-unsupported-llms/versions/1/report")

    assert sets.json()["evaluation_sets"][0]["case_count"] == 10
    assert acceptance.status_code == 200
    assert acceptance.json()["expected_case_count"] == 10
    assert acceptance.json()["complete_human_coverage"] is False
    assert decisions.json() == {"decisions": []}
    assert created.status_code == 201
    assert created.json()["execution_available"] is True
    assert started.status_code == 202
    assert state == "completed"
    assert report.status_code == 200
    assert report.json()["identities_revealed"] is False
    assert "candidates" not in report.json()["definition"]
    trials = report.json()["trials"]
    assert len(trials) == 2
    assert all(trial["success"] is False for trial in trials)
    assert all(set(trial) == {"case_id", "success"} for trial in trials)
    durable_runs = application.list_runs()
    assert len(durable_runs) == 2
    assert all(run["status"] == "failed" for run in durable_runs)
    projection = LocalCatalog(tmp_path / "runs" / ".whoopy-catalog.sqlite3").lab_projection()
    assert projection["experiments"][0]["status"] == "incomplete"
    assert projection["experiments"][0]["report_path"].endswith("report.json")


def test_failed_generation_attempts_are_bounded_and_path_safe(tmp_path: Path) -> None:
    application = _application(tmp_path)
    record = application.record_failed_generation("A grounding meditation", "draft failed")
    attempt_root = (
        application.runs_directory
        / ".generation-workspaces"
        / str(record.run_id)
        / "raw-model-output"
    )
    attempt_root.mkdir(parents=True)
    attempt_root.joinpath("draft-01.json").write_text(
        json.dumps(
            {
                "stage": "draft",
                "attempt": 1,
                "validation_error": "section contains a sentence above the reviewed limit",
                "text": "Raw rejected draft",
                "elapsed_seconds": 0.25,
                "request_seed": 42,
            }
        ),
        encoding="utf-8",
    )
    attempt_root.joinpath("broken.json").write_text("not-json", encoding="utf-8")

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/v1/runs/{record.run_id}/generation-attempts")
        invalid = client.get("/api/v1/runs/not-a-uuid/generation-attempts")

    assert response.status_code == 200
    assert response.json()["attempts"] == [
        {
            "filename": "draft-01.json",
            "stage": "draft",
            "attempt": 1,
            "validation_error": "section contains a sentence above the reviewed limit",
            "text": "Raw rejected draft",
            "elapsed_seconds": 0.25,
            "request_seed": 42,
        }
    ]
    assert invalid.status_code == 422


def test_lab_does_not_execute_saved_definition_until_explicitly_requested(
    tmp_path: Path,
) -> None:
    evaluation_directory = tmp_path / "config" / "evaluation"
    evaluation_directory.mkdir(parents=True)
    evaluation_directory.joinpath("local-v1.yaml").write_text(
        Path("config/evaluation/local-v1.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    application = _application(tmp_path)
    first = _unsupported_llm_experiment()
    first["experiment_id"] = "explicit-first"
    second = _unsupported_llm_experiment()
    second["experiment_id"] = "saved-not-requested"

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        first_created = client.post("/api/v1/lab/experiments", json=first)
        second_created = client.post("/api/v1/lab/experiments", json=second)
        started = client.post(
            "/api/v1/lab/experiments/explicit-first/versions/1/execute",
            json={"case_ids": ["grounding-3m"]},
        )
        deadline = time.monotonic() + 5
        experiments: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            payload = client.get("/api/v1/lab/experiments").json()
            assert isinstance(payload, dict)
            experiments = cast(list[dict[str, Any]], payload["experiments"])
            by_id = {item["definition"]["experiment_id"]: item for item in experiments}
            first_queue = by_id["explicit-first"]["queue"]
            if first_queue is not None and first_queue["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)

    by_id = {item["definition"]["experiment_id"]: item for item in experiments}
    assert first_created.status_code == 201
    assert second_created.status_code == 201
    assert first_created.json()["queue"] is None
    assert second_created.json()["queue"] is None
    assert started.status_code == 202
    assert by_id["explicit-first"]["queue"]["status"] == "completed"
    assert by_id["saved-not-requested"]["queue"] is None
    assert by_id["saved-not-requested"]["report_available"] is False
