"""
One-off TikTok OAuth, run from the terminal.

TikTok has no equivalent of Google's InstalledAppFlow, so this is the small
amount of plumbing that flow does for you: serve a loopback address, send the
operator to the consent page, catch the redirect, and trade the code for a
token pair.

It exists as a command rather than a script because the grant is not permanent.
Access tokens last a day and the publisher refreshes them silently, but the
refresh token expires too, and when it does the only way back is to repeat
this. A documented command beats rediscovering the flow a year from now.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from . import env
from .config import Config

AUTHORIZE = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"

#: Fixed so the operator registers exactly one redirect URI. TikTok's desktop
#: Login Kit permits loopback addresses; the web one requires HTTPS.
PORT = 8723
REDIRECT_URI = f"http://localhost:{PORT}/callback"

#: video.publish is direct posting; video.upload puts drafts in the inbox.
#: user.info.basic is required alongside either for creator_info to answer.
SCOPES = "user.info.basic,video.publish,video.upload"

_PAGE = """<!doctype html><meta charset="utf-8">
<body style="font:16px system-ui;padding:3rem;background:#111;color:#eee">
<h2>{title}</h2><p>{body}</p><p style="color:#888">You can close this tab.</p>
"""


class _Handler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None
    state: str = ""

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if query.get("state", [""])[0] != _Handler.state:
            _Handler.error = "state mismatch (possible CSRF); nothing was saved"
        elif "error" in query:
            _Handler.error = (
                f"{query['error'][0]}: {query.get('error_description', [''])[0]}"
            )
        else:
            _Handler.code = query.get("code", [None])[0]
            if not _Handler.code:
                _Handler.error = "no authorisation code in the redirect"

        ok = _Handler.error is None
        page = _PAGE.format(
            title="Authorised" if ok else "Authorisation failed",
            body="Token saved. Back to the terminal." if ok else _Handler.error,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def log_message(self, *args):
        pass  # the default handler logs every request to stderr


def authorise(cfg: Config, *, verbose: bool = True) -> bool:
    """Runs the consent flow and writes the token file. Returns success."""
    env.ensure_loaded()

    key = os.environ.get("TIKTOK_CLIENT_KEY")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    if not key or not secret:
        print("  !! TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET not set in .env")
        return False

    _Handler.code = _Handler.error = None
    _Handler.state = secrets.token_urlsafe(16)

    # PKCE is mandatory for desktop clients. Note the encoding: TikTok wants the
    # SHA256 of the verifier as lowercase hex, not the base64url that every
    # other OAuth provider uses. Sending base64url fails with a code_challenge
    # error on the consent page, before the user can approve anything.
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()

    url = AUTHORIZE + "?" + urllib.parse.urlencode({
        "client_key": key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": _Handler.state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    try:
        server = HTTPServer(("localhost", PORT), _Handler)
    except OSError as exc:
        print(f"  !! cannot listen on {REDIRECT_URI}: {exc}")
        return False

    print(f"  redirect URI : {REDIRECT_URI}")
    print("  opening the consent page; approve as the account that posts.\n")
    print(f"  If no browser opens, paste this:\n  {url}\n")
    webbrowser.open(url)

    server.timeout = 300
    while _Handler.code is None and _Handler.error is None:
        server.handle_request()
    server.server_close()

    if _Handler.error:
        print(f"  !! {_Handler.error}")
        return False

    response = requests.post(
        TOKEN,
        data={
            "client_key": key,
            "client_secret": secret,
            "code": _Handler.code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    body = response.json()
    if "access_token" not in body:
        print(f"  !! token exchange failed: {json.dumps(body)[:400]}")
        return False

    # expires_at rather than expires_in: the publisher checks it against the
    # clock on every post, and a relative value is meaningless once stored.
    body["expires_at"] = time.time() + float(body.get("expires_in", 0))

    path = cfg.publish.tiktok_token
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")

    if verbose:
        print(f"  saved {path}")
        print(f"  scopes granted: {body.get('scope')}")
    return True
