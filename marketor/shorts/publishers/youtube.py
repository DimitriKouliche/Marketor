"""
YouTube Shorts via the YouTube Data API v3.

The only platform here that needs no approval process — a Google Cloud project
with the Data API enabled and an OAuth client is enough. Quota is the real
limit: an upload costs 1600 units against a 10,000/day default, so roughly six
uploads a day unless you request more.

A vertical video under three minutes is treated as a Short automatically; there
is no flag to set.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Config
from .base import PostCopy, PublishError, PublishResult

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# YouTube truncates hard at these; better to trim than to be rejected.
MAX_TITLE = 100
MAX_DESCRIPTION = 5000


class YouTubePublisher:
    name = "youtube"

    def available(self, cfg: Config) -> tuple[bool, str]:
        try:
            import google.auth  # noqa: F401
            import googleapiclient  # noqa: F401
        except ImportError:
            return False, "google-api-python-client / google-auth-oauthlib not installed"

        if not cfg.publish.youtube_client_secrets.exists():
            return False, (
                f"{cfg.publish.youtube_client_secrets} not found — create an OAuth "
                "client (Desktop app) in Google Cloud Console with the YouTube "
                "Data API v3 enabled and download it there"
            )

        # Say so when the next run will open a browser. This used to report a
        # bare "ok", and the consent screen then arrived unannounced — which
        # matters when several Google accounts are signed in, because the
        # channel it binds to is whichever one happens to be active.
        if not cfg.publish.youtube_token.exists():
            return True, (
                "credentials ok — first post opens a browser to authorise; "
                "sign in as the channel owner"
            )
        return True, ""

    def _credentials(self, cfg: Config):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        token_path = cfg.publish.youtube_token
        creds = None

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(cfg.publish.youtube_client_secrets), SCOPES
                )
                # Opens a browser once; the refresh token is reused after that.
                creds = flow.run_local_server(port=0)

            token_path.write_text(creds.to_json(), encoding="utf-8")

        return creds

    def publish(self, cfg: Config, video: Path, copy: PostCopy) -> PublishResult:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        if not video.exists():
            raise PublishError(f"{video} not found")

        service = build("youtube", "v3", credentials=self._credentials(cfg))

        body = {
            "snippet": {
                "title": copy.title[:MAX_TITLE],
                "description": copy.caption_with_tags[:MAX_DESCRIPTION],
                "categoryId": cfg.publish.youtube_category_id,
                # Tags are separate from in-description hashtags; strip the '#'.
                "tags": [t.lstrip("#") for t in copy.hashtags][:15],
            },
            "status": {
                "privacyStatus": cfg.publish.youtube_privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(str(video), chunksize=-1, resumable=True,
                                mimetype="video/mp4")

        try:
            request = service.videos().insert(
                part="snippet,status", body=body, media_body=media
            )
            response = None
            while response is None:
                _, response = request.next_chunk()
        except HttpError as exc:
            detail = exc.content.decode("utf-8", "replace") if exc.content else str(exc)
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise PublishError(f"YouTube rejected the upload: {detail}") from exc

        video_id = response.get("id")
        if not video_id:
            raise PublishError(f"YouTube returned no video id: {response}")

        return PublishResult(
            platform=self.name,
            remote_id=video_id,
            url=f"https://youtube.com/shorts/{video_id}",
        )
