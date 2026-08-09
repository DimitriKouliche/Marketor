"""Shared types for the platform publishers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..config import Config


class PublishError(RuntimeError):
    """Raised when a platform refuses a post. The queue records and moves on."""


@dataclass
class PostCopy:
    title: str
    caption: str
    hashtags: list[str] = field(default_factory=list)

    @property
    def caption_with_tags(self) -> str:
        tags = " ".join(self.hashtags)
        return f"{self.caption}\n\n{tags}".strip() if tags else self.caption


@dataclass
class PublishResult:
    platform: str
    remote_id: str | None = None
    url: str | None = None


class Publisher(Protocol):
    name: str

    def available(self, cfg: Config) -> tuple[bool, str]:
        """(ready, reason). Reason explains what's missing when not ready."""
        ...

    def publish(self, cfg: Config, video: Path, copy: PostCopy) -> PublishResult:
        ...


def require_env(*names: str) -> tuple[bool, str]:
    """Convenience for publishers whose credentials live in the environment."""
    import os

    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        return False, f"missing environment variable(s): {', '.join(missing)}"
    return True, ""
