"""
Pulls performance numbers back from the platforms.

Without this the experiment loop is just random variation — the whole point is
attributing results to the variant that produced them.

Implemented for YouTube, which needs no approval. TikTok and
Instagram expose insights too, but behind the same approvals as posting, so
they are added when those land.

Different platforms count different things. Everything is normalised to a common
shape, and `primary` is whichever number is the fairest basis for comparison on
that platform — reach where it is available, engagement where it is not.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

from . import db
from .config import Config


def _normalise(
    *, views: int | None = None, likes: int = 0, comments: int = 0, shares: int = 0
) -> dict[str, Any]:
    return {
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        # Reach when the platform reports it; engagement otherwise. Comparing a
        # view count against a like count across platforms would be nonsense, so
        # the learning report only ever compares within one platform.
        "primary": views if views is not None else likes + comments + shares,
    }


def _youtube(cfg: Config, remote_ids: list[str]) -> dict[str, dict[str, Any]]:
    from googleapiclient.discovery import build

    from .publishers.youtube import YouTubePublisher

    service = build("youtube", "v3", credentials=YouTubePublisher()._credentials(cfg))
    out: dict[str, dict[str, Any]] = {}

    # The API takes up to 50 ids per call.
    for i in range(0, len(remote_ids), 50):
        batch = remote_ids[i:i + 50]
        response = service.videos().list(part="statistics", id=",".join(batch)).execute()

        for item in response.get("items", []):
            stats = item.get("statistics", {})
            out[item["id"]] = _normalise(
                views=int(stats.get("viewCount", 0)),
                likes=int(stats.get("likeCount", 0)),
                comments=int(stats.get("commentCount", 0)),
            )

    return out


FETCHERS = {
    "youtube": _youtube,
}


def fetch(cfg: Config, *, verbose: bool = True) -> int:
    """Refreshes metrics for every posted item we know how to measure."""
    updated = 0

    with db.connect(cfg.paths.database) as conn:
        rows = conn.execute(
            "SELECT id, platform, remote_id FROM posts "
            "WHERE state = 'posted' AND remote_id IS NOT NULL"
        ).fetchall()

        by_platform: dict[str, list] = {}
        for row in rows:
            by_platform.setdefault(row["platform"], []).append(row)

        for platform, posts in by_platform.items():
            fetcher = FETCHERS.get(platform)
            if fetcher is None:
                if verbose:
                    print(f"  -- {platform}: no metrics support yet")
                continue

            try:
                results = fetcher(cfg, [p["remote_id"] for p in posts])
            except Exception as exc:
                # One platform being down must not stop the others.
                print(f"  !! {platform} metrics failed: {exc}")
                continue

            for post in posts:
                stats = results.get(post["remote_id"])
                if stats is None:
                    continue
                conn.execute(
                    "UPDATE posts SET metrics = ?, metrics_at = ? WHERE id = ?",
                    (json.dumps(stats), db.now(), post["id"]),
                )
                updated += 1

            if verbose:
                print(f"  + {platform}: {len(results)} of {len(posts)} post(s) measured")

    return updated
