from __future__ import annotations

import base64
import hashlib
import http.client
import json
import threading
from collections.abc import Iterator
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from whoopy.webui.server import LocalWebApplication, _handler_factory
from whoopy.webui.studio import studio_generation_request


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.modules: list[str] = []
        self.scripts: list[str] = []
        self.in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "link" and values.get("rel") == "modulepreload":
            self.modules.append(str(values["href"]))
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.scripts.append(data)


@pytest.fixture
def studio(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> Iterator[tuple[ThreadingHTTPServer, LocalWebApplication]]:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    monkeypatch.setattr(
        application,
        "environment_status",
        lambda: {"ok": True, "speech_models": {"kokoro": {"ready": True}}},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, application
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    server: ThreadingHTTPServer,
    path: str,
    *,
    payload: Any = None,
    origin: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    connection.request(
        "GET" if payload is None else "POST",
        path,
        body=None if payload is None else json.dumps(payload),
        headers=headers,
    )
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_default_studio_bootstrap_assets_and_security_policy(
    studio: tuple[ThreadingHTTPServer, LocalWebApplication],
) -> None:
    server, _ = studio
    status, headers, body = request(server, "/")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    document = Document()
    document.feed(body.decode())
    assert document.modules
    assert document.scripts
    for script in document.scripts:
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in headers["Content-Security-Policy"]
    for module in document.modules:
        status, headers, body = request(server, module)
        assert status == 200
        assert "javascript" in headers["Content-Type"]
        assert body
    for path, content_type in [
        ("/whoopy-resting-v9.png", "image/png"),
        ("/backgrounds/contour-dark.svg", "image/svg+xml"),
        ("/backgrounds/contour-light.svg", "image/svg+xml"),
    ]:
        status, headers, body = request(server, path)
        assert status == 200
        assert headers["Content-Type"] == content_type
        assert body
    for path in ["/?tab=settings", "/backgrounds/"]:
        status, _, body = request(server, path)
        assert status == 200
        document = Document()
        document.feed(body.decode())
        assert document.modules


@pytest.mark.parametrize(
    "path",
    ["/../pyproject.toml", "/%2e%2e/pyproject.toml", "/.git/config", "/api/v1/unknown"],
)
def test_studio_does_not_expose_files_outside_public_assets(
    studio: tuple[ThreadingHTTPServer, LocalWebApplication], path: str
) -> None:
    assert request(studio[0], path)[0] == 404


def test_versioned_status_describes_supported_controls_without_changing_legacy_api(
    studio: tuple[ThreadingHTTPServer, LocalWebApplication], monkeypatch: MonkeyPatch
) -> None:
    server, application = studio
    status, _, body = request(server, "/api/v1/status")
    environment = json.loads(body)
    assert status == 200
    assert environment["writing_profiles"] == ["standard"]
    model = environment["speech_models"]["kokoro"]
    assert model["studio_eligible"] is True
    assert set(model["capabilities"]["preset_values"]) == {
        "af_bella",
        "af_heart",
        "af_nicole",
        "am_michael",
    }
    assert json.loads(request(server, "/api/status")[2]) == {
        "ok": True,
        "speech_models": {"kokoro": {"ready": True}},
    }
    monkeypatch.setattr(
        application,
        "environment_status",
        lambda: {"ok": True, "speech_models": {"kokoro": {"ready": False}}},
    )
    assert (
        json.loads(request(server, "/api/v1/status")[2])["speech_models"]["kokoro"][
            "studio_eligible"
        ]
        is False
    )
    monkeypatch.setattr(
        application,
        "environment_status",
        lambda: {
            "ok": True,
            "profiles": {"basic": {"installed": True, "compatible": True}},
            "speech_models": {"kokoro": {"ready": False, "error": "missing optional pack"}},
        },
    )
    model = json.loads(request(server, "/api/v1/status")[2])["speech_models"]["kokoro"]
    assert model["studio_eligible"] is True
    assert model["ready"] is True
    assert model["error"] is None
    assert json.loads(request(server, "/api/v1/runs?audience=studio")[2]) == {"runs": []}


def test_versioned_generation_uses_existing_service_and_preserves_origin_boundary(
    studio: tuple[ThreadingHTTPServer, LocalWebApplication], monkeypatch: MonkeyPatch
) -> None:
    server, application = studio
    captured: list[dict[str, Any]] = []

    def start(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(payload)
        return {"task_id": "local-task", "status": "queued"}

    monkeypatch.setattr(application, "start_generation", start)
    payload = {"mode": "prompt", "text": "Help me unwind.", "pace": "slow"}
    assert (
        request(server, "/api/v1/generate", payload=payload, origin="https://example.com")[0] == 403
    )
    assert captured == []
    status, _, body = request(
        server,
        "/api/v1/generate",
        payload=payload,
        origin=f"http://127.0.0.1:{server.server_port}",
    )
    assert status == 202
    assert json.loads(body)["task_id"] == "local-task"
    assert captured == [{**payload, "speed": 0.85}]


def test_studio_preferences_keep_validated_request_limits() -> None:
    assert studio_generation_request(None) is None
    assert studio_generation_request({"speed": 0.9, "pace": "slow"})["speed"] == 0.9
    assert studio_generation_request({"pace": "natural"})["speed"] == 1.0
    assert studio_generation_request({"meditation_kind": [], "text": "Rest."})["text"] == ("Rest.")
    assert (
        studio_generation_request({"meditation_kind": "sleep", "text": "Rest."})["text"]
        == "Rest.\n\nHelp me settle for sleep."
    )
    text = "x" * 20_000
    assert studio_generation_request({"meditation_kind": "sleep", "text": text})["text"] == text
