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

# Upload is not enough on its own: reading back a video's own statistics goes
# through videos.list, which refuses an upload-only token with "insufficient
# authentication scopes". The metrics fetcher was written against this API and
# had never once succeeded because of it.
#: Without these, this class cannot do its job. upload posts the video;
#: readonly is what videos.list needs to read a video's own statistics back.
REQUIRED_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

#: Nice to have, and strictly separate from the two above. yt-analytics is the
#: only way to tell a video nobody was shown from a video everybody skipped.
OPTIONAL_SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

#: What a fresh authorisation asks for.
SCOPES = REQUIRED_SCOPES + OPTIONAL_SCOPES

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

    def _credentials(self, cfg: Config, *, reauthorise: bool = False):
        """
        A usable token, refreshed against the scopes it was actually granted.

        Refreshing against this module's SCOPES instead is what broke uploading
        for six days: adding yt-analytics to the list meant every refresh asked
        Google for a scope the saved grant did not carry, and Google answers
        that with `invalid_scope` — which reads as a broken account rather than
        as a stale token, and names no remedy. A grant knows its own scopes;
        they are the only correct thing to refresh with.

        So an optional scope can no longer take posting down with it. Adding one
        now costs an un-upgraded token that still uploads, until someone runs
        `shorts youtube-auth`.
        """
        import json

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        token_path = cfg.publish.youtube_token
        creds = None

        if token_path.exists() and not reauthorise:
            try:
                granted = json.loads(token_path.read_text(encoding="utf-8")).get("scopes")
            except (json.JSONDecodeError, OSError):
                granted = None
            creds = Credentials.from_authorized_user_file(
                str(token_path), granted or REQUIRED_SCOPES
            )

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                token_path.write_text(creds.to_json(), encoding="utf-8")
                return creds
            except Exception as exc:
                # A grant that cannot be refreshed is a grant that is gone;
                # say so rather than opening a browser in a scheduled run that
                # nobody is watching.
                raise PublishError(
                    f"YouTube authorisation expired and could not be refreshed "
                    f"({exc}). Run `shorts youtube-auth` to sign in again."
                ) from exc

        if not reauthorise and token_path.exists():
            raise PublishError(
                "YouTube token is unusable. Run `shorts youtube-auth`."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(cfg.publish.youtube_client_secrets), SCOPES
        )
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
                "tags": [t.lstrip("#") for t in copy.unique_hashtags][:15],
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
