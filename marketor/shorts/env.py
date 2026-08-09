"""
Loads .env into the process environment.

The publishers read their credentials from os.environ, so something has to put
them there. A small parser rather than a dependency: the file format is a few
KEY=value lines and the existing .env already uses spaces around the '=' and
quoted values, both of which are handled here.

Real environment variables always win — an exported value should override the
file, not the other way round.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_loaded = False


def load_env(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """
    Reads a .env file and sets any keys not already in the environment.

    Returns the values it applied, so callers can report what was picked up.
    Missing file is not an error — most keys are optional.
    """
    global _loaded

    path = Path(path) if path else DEFAULT_ENV_PATH
    applied: dict[str, str] = {}

    if not path.exists():
        _loaded = True
        return applied

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # Tolerate the "export KEY=value" form.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        key, sep, value = line.partition("=")
        if not sep:
            continue

        key = key.strip()
        value = value.strip()

        # Strip one layer of matching quotes, then any trailing comment on an
        # unquoted value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()

        if not key:
            continue
        if key in os.environ and not override:
            continue

        os.environ[key] = value
        applied[key] = value

    _loaded = True
    return applied


def ensure_loaded() -> None:
    """Loads .env once per process. Safe to call from any entry point."""
    if not _loaded:
        load_env()
