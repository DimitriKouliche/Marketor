"""
TikTok via the Content Posting API, in one of two modes.

`direct` posts straight to the profile, and needs an audited app. Ours was
refused: TikTok told us in as many words that "TikTok for Developers currently
does not support personal or internal company use", which is exactly what
posting our own game to our own account is. That is a policy, not a form we
filled in wrong, so reapplying with the same premise gets the same answer. An
unaudited app can still call the endpoint, but every post is forced to
SELF_ONLY — visible to nobody, which is not publishing.

`inbox` is the mode that works, and the default. The video is uploaded to the
account's TikTok drafts, where the creator opens the notification, writes the
caption and posts. The API never publishes, a person does, which is the whole
reason TikTok allows it without an audit.

The cost is the caption: the inbox endpoint accepts source_info and nothing
else, so the copy this pipeline writes cannot travel with the video. It has to
be carried over by hand, which is why `handoff` prints it.

Credentials come from the environment (TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET)
and a token file holding the OAuth grant.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import requests

from ..config import Config
from .base import PostCopy, PublishError, PublishResult, require_env

API = "https://open.tiktokapis.com/v2"

# TikTok requires chunks between 5 MB and 64 MB, except for a single-chunk
# upload which may be the whole file if it is under 64 MB.
CHUNK_SIZE = 10 * 1024 * 1024
SINGLE_CHUNK_LIMIT = 64 * 1024 * 1024

MAX_CAPTION = 2200


class TikTokPublisher:
    name = "tiktok"

    def available(self, cfg: Config) -> tuple[bool, str]:
        ok, reason = require_env("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET")
        if not ok:
            return False, reason
        if not cfg.publish.tiktok_token.exists():
            return False, (
                f"{cfg.publish.tiktok_token} not found — complete the OAuth flow "
                "once and save {access_token, refresh_token} there"
            )
        return True, ""

    def _token(self, cfg: Config) -> str:
        """Returns a valid access token, refreshing and re-saving if expired."""
        path = cfg.publish.tiktok_token
        data = json.loads(path.read_text(encoding="utf-8"))

        if data.get("expires_at", 0) > time.time() + 60:
            return data["access_token"]

        response = requests.post(
            f"{API}/oauth/token/",
            data={
                "client_key": os.environ["TIKTOK_CLIENT_KEY"],
                "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if response.status_code != 200:
            raise PublishError(f"TikTok token refresh failed: {response.text}")

        # TikTok answers 200 with the failure in the body, so the status check
        # above never fires and payload["access_token"] used to die on a bare
        # KeyError reading '"access_token"' — which says nothing about what
        # went wrong or what to do about it.
        payload = response.json()
        if "access_token" not in payload:
            detail = payload.get("error_description") or payload.get("error") or payload
            hint = ""
            if payload.get("error") == "invalid_grant":
                hint = (
                    " — the saved grant no longer works; run "
                    "`shorts tiktok-auth` to authorise again. Note a grant "
                    "belongs to the app that issued it, so swapping the "
                    "sandbox keys for production ones invalidates it too."
                )
            raise PublishError(f"TikTok token refresh failed: {detail}{hint}")
        data.update(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", data["refresh_token"]),
            expires_at=time.time() + int(payload.get("expires_in", 86400)),
        )
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data["access_token"]

    def _creator_info(self, token: str) -> dict:
        """
        Queries the creator's posting options.

        Required before a direct post, and it is how you find out whether the
        audit has actually lifted the SELF_ONLY restriction.
        """
        response = requests.post(
            f"{API}/post/publish/creator_info/query/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise PublishError(f"TikTok creator_info failed: {response.text}")
        return response.json().get("data", {})

    def publish(self, cfg: Config, video: Path, copy: PostCopy) -> PublishResult:
        if not video.exists():
            raise PublishError(f"{video} not found")

        token = self._token(cfg)
        if getattr(cfg.publish, "tiktok_mode", "inbox") == "inbox":
            return self._to_inbox(token, video)

        info = self._creator_info(token)

        allowed = info.get("privacy_level_options") or ["SELF_ONLY"]
        privacy = cfg.publish.tiktok_privacy
        if privacy not in allowed:
            # Falling back rather than failing: an unaudited app legitimately
            # only has SELF_ONLY, and a private post beats no post.
            privacy = "SELF_ONLY" if "SELF_ONLY" in allowed else allowed[0]

        size = video.stat().st_size
        chunk_size, chunk_count = self._chunking(size)

        init = requests.post(
            f"{API}/post/publish/video/init/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={
                "post_info": {
                    "title": copy.caption_with_tags[:MAX_CAPTION],
                    "privacy_level": privacy,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": chunk_count,
                },
            },
            timeout=60,
        )
        if init.status_code != 200:
            raise PublishError(f"TikTok init failed: {init.text}")

        payload = init.json().get("data", {})
        publish_id = payload.get("publish_id")
        upload_url = payload.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError(f"TikTok init returned no upload target: {init.text}")

        self._upload(video, upload_url, size, chunk_size, chunk_count)
        self._await_publish(token, publish_id)

        return PublishResult(
            platform=self.name,
            remote_id=publish_id,
            url=None,  # TikTok does not return a post URL from this endpoint.
        )

    def _chunking(self, size: int) -> tuple[int, int]:
        if size <= SINGLE_CHUNK_LIMIT:
            return size, 1
        return CHUNK_SIZE, math.ceil(size / CHUNK_SIZE)

    def _to_inbox(self, token: str, video: Path) -> PublishResult:
        """
        Uploads to the account's drafts. No caption: the endpoint takes
        source_info only, and the creator writes the text in the app.
        """
        size = video.stat().st_size
        chunk_size, chunk_count = self._chunking(size)

        init = requests.post(
            f"{API}/post/publish/inbox/video/init/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": chunk_count,
                },
            },
            timeout=60,
        )
        if init.status_code != 200:
            raise PublishError(f"TikTok inbox init failed: {init.text}")

        payload = init.json().get("data", {})
        publish_id = payload.get("publish_id")
        upload_url = payload.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError(f"TikTok inbox returned no upload target: {init.text}")

        self._upload(video, upload_url, size, chunk_size, chunk_count)
        self._await_publish(token, publish_id)

        return PublishResult(
            platform=self.name,
            remote_id=publish_id,
            url=None,  # A draft has no URL until the creator posts it.
        )

    def _upload(self, video: Path, url: str, size: int, chunk_size: int, count: int) -> None:
        with video.open("rb") as fh:
            for index in range(count):
                start = index * chunk_size
                # The final chunk takes the remainder, which may exceed
                # chunk_size — TikTok expects that rather than a short chunk.
                end = size - 1 if index == count - 1 else start + chunk_size - 1
                fh.seek(start)
                body = fh.read(end - start + 1)

                response = requests.put(
                    url,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{size}",
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(body)),
                    },
                    data=body,
                    timeout=600,
                )
                if response.status_code not in (200, 201, 206):
                    raise PublishError(
                        f"TikTok chunk {index + 1}/{count} failed "
                        f"({response.status_code}): {response.text}"
                    )

    def _await_publish(self, token: str, publish_id: str, *, timeout: int = 600) -> None:
        """Polls until TikTok finishes processing, so failures surface here."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            response = requests.post(
                f"{API}/post/publish/status/fetch/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json={"publish_id": publish_id},
                timeout=30,
            )
            if response.status_code != 200:
                raise PublishError(f"TikTok status check failed: {response.text}")

            data = response.json().get("data", {})
            status = data.get("status")

            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return
            if status == "FAILED":
                raise PublishError(
                    f"TikTok publish failed: {data.get('fail_reason', 'unknown')}"
                )

            time.sleep(5)

        raise PublishError(f"TikTok publish {publish_id} still processing after {timeout}s")
