from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from whoopy.artifacts import ArtifactState
from whoopy.hardware import HardwareSnapshot
from whoopy.webui.api import create_api
from whoopy.webui.server import LocalWebApplication


class EmptyPacks:
    def list(self) -> dict[str, object]:
        return {"packs": []}


@pytest.mark.parametrize("override", [False, True])
def test_status_names_the_actual_configured_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: bool
) -> None:
    config = tmp_path / "config"
    shutil.copytree(Path("config"), config)
    if override:
        lock_path = config / "artifacts.yaml"
        lock = yaml.safe_load(lock_path.read_text())
        writer = next(x for x in lock["artifacts"] if x["component"] == "llm_model_standard")
        writer.update(
            artifact_id="fixture_balanced_model",
            display_name="Test balanced writer",
            version="fixture-2",
        )
        lock_path.write_text(yaml.safe_dump(lock))
    monkeypatch.setattr(
        "whoopy.webui.server.inspect_hardware",
        lambda _path: HardwareSnapshot(
            operating_system="darwin",
            architecture="arm64",
            cpu_count=8,
            total_ram_gb=48,
            available_ram_gb=9,
            free_disk_gb=100,
            accelerators=["cpu", "metal"],
        ),
    )
    monkeypatch.setattr(
        "whoopy.webui.server.ArtifactStore.inspect",
        lambda *_args: SimpleNamespace(state=ArtifactState.MISSING),
    )
    app = LocalWebApplication(
        project_root=tmp_path,
        config_directory=config,
        models_directory=Path("models/managed"),
        runs_directory=Path("runs"),
        model_pack_service_factory=lambda *_args: EmptyPacks(),
    )
    with TestClient(create_api(app), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/status")
    assert response.status_code == 200
    profiles: dict[str, Any] = response.json()["profiles"]
    assert profiles["basic"]["writer_model"] is None
    standard = profiles["standard"]["writer_model"]
    assert standard["display_name"] == ("Test balanced writer" if override else "Qwen3 4B Q4_K_M")
    assert standard["artifact_id"] == ("fixture_balanced_model" if override else "qwen3_4b_q4_k_m")
    assert standard["size_bytes"] > 0
    assert profiles["standard"]["compatible"] is True
    assert profiles["standard"]["installed"] is False
    assert profiles["high"]["compatible"] is False
    assert profiles["high"]["writer_model"]["display_name"] == "Ministral 3 14B Q4_K_M"
