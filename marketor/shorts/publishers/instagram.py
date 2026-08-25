"""
Instagram Reels via the Instagram Graph API.

Meta ships two different Instagram publishing APIs, and which one you have
depends on how you authorised:

- **Instagram Login** — token starts with `IGAA`, served by graph.instagram.com,
  no Facebook Page needed. Get it from the app's Instagram product page.
- **Facebook Login** — token starts with `EAA`, served by graph.facebook.com,
  requires a Business/Creator account linked to a Page.

The request shapes are identical, so the only thing that differs is the host —
picked automatically from the token below. Sending an `IGAA` token to
graph.facebook.com fails with "Cannot parse access token", which reads like a
bad token rather than the wrong host and is a genuinely confusing hour.

Either way the app needs content-publishing permission approved, and
IG_ACCESS_TOKEN must hold a long-lived token.

Publishing is a fetch, not an upload: you hand Meta a public HTTPS URL and it
pulls the file, which is what hosting.py is for. Note the API caps Reels at 90
seconds even though the app allows three minutes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests

from ..config import Config
from . import hosting
from .base import PostCopy, PublishError, PublishResult, require_env

# Meta versions the Graph API and retires versions after roughly two years.
# Override with IG_GRAPH_VERSION when this one is close to its sunset.
DEFAULT_GRAPH_VERSION = "v21.0"

MAX_CAPTION = 2200
REELS_MAX_SECONDS = 90


class InstagramPublisher:
    name = "instagram"

    def available(self, cfg: Config) -> tuple[bool, str]:
        ok, reason = require_env("IG_ACCESS_TOKEN")
        if not ok:
            return False, reason
        if not cfg.publish.instagram_user_id:
            return False, "publish.instagram_user_id is empty (numeric IG account id)"

        # Publishing is a fetch, not an upload, so hosting is a hard dependency
        # rather than an implementation detail.
        return hosting.check(cfg)

    @property
    def _base(self) -> str:
        version = os.environ.get("IG_GRAPH_VERSION", DEFAULT_GRAPH_VERSION)
        return f"{self._host}/{version}"

    @property
    def _host(self) -> str:
        """
        Which Graph host serves this token.

        Instagram Login tokens are prefixed `IGAA` and are only valid against
        graph.instagram.com; Facebook Page tokens are prefixed `EAA` and only
        valid against graph.facebook.com. Neither host accepts the other's
        token, so this is derived rather than configured — one less setting to
        get wrong, and the failure it prevents is opaque.
        """
        token = os.environ.get("IG_ACCESS_TOKEN", "")
        if token.startswith("IGAA"):
            return "https://graph.instagram.com"
        return "https://graph.facebook.com"

    def publish(self, cfg: Config, video: Path, copy: PostCopy) -> PublishResult:
        if not video.exists():
            raise PublishError(f"{video} not found")

        token = os.environ["IG_ACCESS_TOKEN"]
        user_id = cfg.publish.instagram_user_id

        video_url = hosting.public_url_for(cfg, video)

        container = requests.post(
            f"{self._base}/{user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": copy.caption_with_tags[:MAX_CAPTION],
                "share_to_feed": "true",
                "access_token": token,
            },
            timeout=120,
        )
        if container.status_code != 200:
            raise PublishError(f"Instagram container create failed: {container.text}")

        creation_id = container.json().get("id")
        if not creation_id:
            raise PublishError(f"Instagram returned no container id: {container.text}")

        self._await_container(creation_id, token)

        published = requests.post(
            f"{self._base}/{user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
            timeout=120,
        )
        if published.status_code != 200:
            raise PublishError(f"Instagram publish failed: {published.text}")

        media_id = published.json().get("id")
        return PublishResult(
            platform=self.name,
            remote_id=media_id,
            url=f"https://www.instagram.com/reel/{media_id}/" if media_id else None,
        )

    def _await_container(self, creation_id: str, token: str, *, timeout: int = 600) -> None:
        """
        Waits for Meta to finish downloading and transcoding.

        Publishing an unfinished container fails, so this poll is required
        rather than a nicety.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            response = requests.get(
                f"{self._base}/{creation_id}",
                params={"fields": "status_code,status", "access_token": token},
                timeout=30,
            )
            if response.status_code != 200:
                raise PublishError(f"Instagram status check failed: {response.text}")

            data = response.json()
            status = data.get("status_code")

            if status == "FINISHED":
                return
            if status == "ERROR":
                raise PublishError(
                    f"Instagram failed to process the video: {data.get('status', '')}"
                )

            time.sleep(5)

        raise PublishError(f"Instagram container {creation_id} not ready after {timeout}s")
