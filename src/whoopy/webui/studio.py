"""Serve the bundled Studio against the existing local generation service."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from importlib.resources.abc import Traversable
from typing import Any

from whoopy.voices import KOKORO_ENGLISH_VOICES


def studio_environment(environment: dict[str, Any]) -> dict[str, Any]:
    """Describe only controls and voices supported by this generation service."""

    models = dict(environment.get("speech_models", {}))
    kokoro = dict(models.get("kokoro", {}))
    basic = environment.get("profiles", {}).get("basic", {})
    baseline_ready = basic.get("installed") is True and basic.get("compatible") is True
    ready = kokoro.get("ready") is True or baseline_ready
    kokoro.update(
        ready=ready,
        studio_eligible=ready,
        capabilities={
            "preset_values": list(KOKORO_ENGLISH_VOICES),
            "delivery": {"values": ["neutral"], "fallback": "neutral"},
        },
    )
    if ready:
        kokoro["error"] = None
    models["kokoro"] = kokoro
    return {**environment, "speech_models": models, "writing_profiles": ["standard"]}


def studio_generation_request(payload: Any) -> Any:
    """Translate Studio preferences without introducing another generation path."""

    if not isinstance(payload, dict):
        return payload
    request = dict(payload)
    if "speed" not in request:
        request["speed"] = 0.85 if request.get("pace") == "slow" else 1.0
    kind = request.get("meditation_kind")
    focus = (
        {
            "sleep": "Help me settle for sleep.",
            "focused_attention": "Help me practice calm, focused attention.",
            "self_compassion": "Help me practice kindness toward myself.",
        }.get(kind)
        if isinstance(kind, str)
        else None
    )
    text = request.get("text")
    if (
        focus
        and isinstance(text, str)
        and text.strip()
        and len(text.strip()) + len(focus) + 2 <= 20_000
    ):
        request["text"] = f"{text.strip()}\n\n{focus}"
    return request


class StudioAssets:
    """Read packaged assets while keeping requests inside the public bundle."""

    def __init__(self, root: Traversable) -> None:
        self.root = root
        document = root.joinpath("index.html").read_text(encoding="utf-8")
        hashes = {
            "'sha256-"
            + base64.b64encode(hashlib.sha256(source.encode()).digest()).decode("ascii")
            + "'"
            for source in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", document, re.DOTALL)
        }
        script_policy = "script-src 'self' " + " ".join(sorted(hashes))
        self.content_security_policy = (
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            f"style-src 'self' 'unsafe-inline'; {script_policy}"
        )

    def response(self, path: str) -> tuple[bytes, str] | None:
        if "\\" in path or "\x00" in path:
            return None
        parts = path.lstrip("/").split("/")
        if any(part.startswith(".") for part in parts):
            return None
        if path in {"/", "/backgrounds", "/backgrounds/"}:
            parts = ["index.html"]
        asset = self.root.joinpath(*parts)
        if not asset.is_file():
            return None
        content_type = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
        return asset.read_bytes(), content_type
