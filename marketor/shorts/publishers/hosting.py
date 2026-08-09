"""
Makes a rendered clip reachable over public HTTPS.

Only Instagram needs this: its Graph API fetches the video from a URL you give
it rather than accepting an upload. Both backends here are free.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

from ..config import Config
from .base import PublishError

GITHUB_API = "https://api.github.com"


def public_url_for(cfg: Config, video: Path) -> str:
    backend = cfg.publish.hosting.backend

    if backend == "public_url":
        base = cfg.publish.hosting.public_base_url.rstrip("/")
        if not base:
            raise PublishError(
                "hosting.public_base_url is empty; set it to whatever serves "
                "paths.output over HTTPS"
            )
        return f"{base}/{video.name}"

    if backend == "github_release":
        return _upload_to_github_release(cfg, video)

    raise PublishError(f"unknown hosting backend {backend!r}")


def _upload_to_github_release(cfg: Config, video: Path) -> str:
    """
    Uploads the clip as a release asset and returns its download URL.

    A release in a repo you already own is free, durable, and served over HTTPS,
    which is all Instagram's fetcher needs.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise PublishError("GITHUB_TOKEN not set (needs 'contents: write' on the repo)")

    repo = cfg.publish.hosting.github_repo
    if not repo:
        raise PublishError("hosting.github_repo is empty; set it to 'owner/repo'")

    tag = cfg.publish.hosting.github_release_tag
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    release = requests.get(
        f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", headers=headers, timeout=30
    )

    if release.status_code == 404:
        release = requests.post(
            f"{GITHUB_API}/repos/{repo}/releases",
            headers=headers,
            json={
                "tag_name": tag,
                "name": "Shorts hosting",
                "body": "Asset host for the automated shorts pipeline.",
                "draft": False,
                "prerelease": True,
            },
            timeout=30,
        )

    if release.status_code not in (200, 201):
        raise PublishError(f"GitHub release lookup/create failed: {release.text}")

    data = release.json()
    release_id = data["id"]

    # Re-uploading the same asset name is a 422; delete the old one first so a
    # re-run of a clip doesn't dead-end.
    for asset in data.get("assets", []):
        if asset["name"] == video.name:
            requests.delete(
                f"{GITHUB_API}/repos/{repo}/releases/assets/{asset['id']}",
                headers=headers, timeout=30,
            )

    upload_host = data["upload_url"].split("{")[0]
    with video.open("rb") as fh:
        upload = requests.post(
            f"{upload_host}?name={video.name}",
            headers={**headers, "Content-Type": "video/mp4"},
            data=fh,
            timeout=900,
        )

    if upload.status_code not in (200, 201):
        raise PublishError(f"GitHub asset upload failed: {upload.text}")

    return upload.json()["browser_download_url"]
