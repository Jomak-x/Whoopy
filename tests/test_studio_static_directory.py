from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whoopy.webui.api import create_api
from whoopy.webui.server import LocalWebApplication


def _application(root: Path) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=root,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )


def _studio(root: Path, script: str) -> Path:
    root.mkdir()
    (root / "index.html").write_text(f"<html><body><script>{script}</script></body></html>")
    immutable = root / "_app" / "immutable"
    immutable.mkdir(parents=True)
    (immutable / "fixture.js").write_text("export const loaded = true;")
    return root


def _hash(script: str) -> str:
    return base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()


def test_each_studio_bundle_has_its_own_exact_bootstrap_policy(tmp_path: Path) -> None:
    first = "window.firstStudio = true;"
    second = "window.secondStudio = true;"
    roots = [_studio(tmp_path / "one", first), _studio(tmp_path / "two", second)]
    clients = [
        TestClient(
            create_api(_application(tmp_path / str(i)), static_directory=root),
            base_url="http://127.0.0.1",
        )
        for i, root in enumerate(roots)
    ]
    for i, client in enumerate(clients):
        response = client.get("/")
        assert response.status_code == 200
        assert [first, second][i] in response.text
        policy = response.headers["content-security-policy"]
        assert f"'sha256-{_hash([first, second][i])}'" in policy
        assert f"'sha256-{_hash([first, second][1 - i])}'" not in policy
        assert response.headers["cache-control"] == "no-store"
        asset = client.get("/_app/immutable/fixture.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert asset.headers["x-content-type-options"] == "nosniff"
        denied = client.post("/api/v1/generate", json={}, headers={"Origin": "http://evil.test"})
        assert denied.status_code == 403
        assert denied.headers["cache-control"] == "no-store"
        assert denied.headers["x-content-type-options"] == "nosniff"
        assert denied.headers["content-security-policy"] == policy
        oversized = client.post(
            "/api/v1/generate", content="{}", headers={"Content-Length": "64001"}
        )
        assert oversized.status_code == 413
        assert oversized.headers["content-security-policy"] == policy
        malformed = client.post(
            "/api/v1/generate", content="{}", headers={"Content-Length": "invalid"}
        )
        assert malformed.status_code == 400
        assert malformed.headers["x-content-type-options"] == "nosniff"


def test_bad_static_root_fails_before_creating_local_catalog(tmp_path: Path) -> None:
    app = _application(tmp_path / "data")
    with pytest.raises(ValueError, match=r"index\.html"):
        create_api(app, static_directory=tmp_path / "missing")
    assert not (app.runs_directory / ".whoopy-catalog.sqlite3").exists()


@pytest.mark.parametrize("command", ["app", "web"])
def test_cli_forwards_explicit_local_bundle(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from whoopy.cli import main

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("whoopy.webui.server.serve", lambda **kwargs: calls.append(kwargs))
    args = [command, "--static-directory", str(tmp_path), "--port", "8775"]
    if command == "app":
        args.append("--no-open")
    assert main(args) == 0
    assert calls[0]["static_directory"] == tmp_path
    assert calls[0]["port"] == 8775
    assert calls[0]["open_browser"] is False
