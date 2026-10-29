"""Check accessibility prerequisites on the actual served Studio document.

Studio now renders on the client. Retired source-string assertions about Lab
panels, reference recording, dialogs, and waveform controls were not behavioral
tests and cannot establish accessibility of the new UI. Interactive keyboard,
focus, reduced-motion, and mobile checks belong in a browser suite.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from whoopy.webui.api import create_api
from whoopy.webui.server import LocalWebApplication


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def test_served_studio_declares_language_page_title_and_allows_mobile_zoom(tmp_path: Path) -> None:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.get("/")

    assert response.status_code == 200
    document = _Document()
    document.feed(response.text)
    html = [attrs for tag, attrs in document.elements if tag == "html"]
    assert len(html) == 1
    assert html[0].get("lang") == "en"
    assert document.title.strip() == "Whoopy Local Studio"
    viewports = [
        attrs["content"]
        for tag, attrs in document.elements
        if tag == "meta" and attrs.get("name") == "viewport"
    ]
    assert len(viewports) == 1
    viewport = viewports[0]
    assert viewport is not None
    directives = dict(part.strip().lower().split("=", 1) for part in viewport.split(","))
    assert directives["width"] == "device-width"
    assert directives.get("user-scalable") not in {"no", "0"}
    if "maximum-scale" in directives:
        assert float(directives["maximum-scale"]) >= 2
