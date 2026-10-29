"""Resolve source-tree and installed-wheel product resources."""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def installed_data_root() -> Path:
    """Return the conventional wheel data root for the active environment."""

    return Path(sys.prefix) / "share" / "whoopy"


def default_config_directory() -> Path:
    """Prefer an explicit checkout config, then the wheel's bundled defaults."""

    checkout = Path("config")
    if (checkout / "default.yaml").is_file():
        return checkout
    installed = installed_data_root() / "config"
    if (installed / "default.yaml").is_file():
        return installed
    # Preserve the established user-facing error when neither distribution is
    # complete instead of silently falling back to partial configuration.
    return checkout


def runtime_worker_script(filename: str) -> Path:
    """Resolve an optional-model worker in a checkout or installed wheel."""

    checkout = SOURCE_PROJECT_ROOT / "scripts" / filename
    if checkout.is_file():
        return checkout
    return installed_data_root() / "scripts" / filename
