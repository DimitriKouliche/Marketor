"""
Pulls performance numbers back from the platforms.

Without this the experiment loop is just random variation — the whole point is
attributing results to the variant that produced them.

Implemented for all three. Instagram needs no extra permission beyond the one
publishing already uses; TikTok needs the `video.list` scope, which comes with
the same app review as posting and is reported plainly until it lands.

Different platforms count different things. Everything is normalised to a common
shape, and `primary` is whichever number is the fairest basis for comparison on
that platform — reach where it is available, engagement where it is not.
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

import requests

from . import db
from .config import Config


def _normalise(
    *,
    views: int | None = None,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saved: int = 0,
    reach: int | None = None,
    watch_seconds: float | None = None,
    clip_seconds: float | None = None,
) -> dict[str, Any]:
    """
    One shape for every platform, with two different questions kept apart.

    Views answer "did it spread", which is mostly the thumbnail, the hour it
    went out and the algorithm. Retention answers "was the clip any good" — how
    much of it people actually watched — and that is the only number here that
    is really about the footage. They are stored separately because tuning
    selection on reach optimises the wrong thing.
    """
    retention = None
    if watch_seconds is not None and clip_seconds:
        retention = round(100.0 * watch_seconds / clip_seconds, 1)

    return {
        "views": views,
        "reach": reach,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saved": saved,
        "watch_seconds": round(watch_seconds, 2) if watch_seconds is not None else None,
        "retention": retention,
        # Reach when the platform reports it; engagement otherwise. Comparing a
        # view count against a like count across platforms would be nonsense, so
        # the learning report only ever compares within one platform.
        "primary": views if views is not None else likes + comments + shares,
        # What the clip itself was worth, independent of how far it travelled.
        # None until the platform reports watch time.
        "quality": retention,
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

    _add_youtube_reach(cfg, out)
    return out


def _add_youtube_reach(cfg: Config, out: dict[str, dict[str, Any]]) -> None:
    """
    Adds impressions and average view duration, where the grant allows it.

    Views alone cannot tell a video nobody was shown from a video everybody
    skipped, and those two failures have opposite fixes: the first is about
    distribution, the second about the first second of the clip. Analytics is
    a separate authorisation, so this stays optional and never fails the run.
    """
    if not out:
        return
    try:
        from googleapiclient.discovery import build

        from .publishers.youtube import YouTubePublisher

        creds = YouTubePublisher()._credentials(cfg)
        if not any("yt-analytics" in s for s in (getattr(creds, "scopes", None) or [])):
            return

        analytics = build("youtubeAnalytics", "v2", credentials=creds)
        for video_id, row in out.items():
            resp = analytics.reports().query(
                ids="channel==MINE",
                startDate="2020-01-01",
                endDate=date.today().isoformat(),
                metrics="views,estimatedMinutesWatched,averageViewDuration,"
                        "averageViewPercentage",
                filters=f"video=={video_id}",
            ).execute()
            rows = resp.get("rows") or []
            if not rows:
                continue
            _, minutes, avg_seconds, avg_percent = rows[0][:4]
            row["watch_seconds"] = avg_seconds
            row["retention"] = avg_percent
            # Retention is the honest quality signal: it does not move with
            # how many people the platform chose to show the clip to.
            row["quality"] = avg_percent
    except Exception as exc:
        print(f"  (youtube analytics unavailable: {exc})")


def _instagram(cfg: Config, remote_ids: list[str]) -> dict[str, dict[str, Any]]:
    """
    Reel insights, one call per media.

    There is no batch endpoint for insights, but the volume here is a handful of
    posts a week rather than thousands, so a call each is the simpler trade.

    Metric names have moved: `plays` became `views`, and asking for a metric a
    given media does not support fails the whole request rather than omitting
    it. So insights are attempted first and the plain counters are used when
    that fails, which is always better than recording nothing.
    """
    from .publishers.instagram import InstagramPublisher

    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("IG_ACCESS_TOKEN is not set")

    base = InstagramPublisher()._base
    out: dict[str, dict[str, Any]] = {}

    with db.connect(cfg.paths.database) as conn:
        lengths = {rid: _clip_seconds(conn, rid) for rid in remote_ids}

    for media_id in remote_ids:
        views = likes = comments = shares = None
        reach = saved = None
        watch_ms = None
        try:
            response = requests.get(
                f"{base}/{media_id}/insights",
                params={
                    "metric": "views,reach,likes,comments,shares,saved,"
                              "ig_reels_avg_watch_time",
                    "access_token": token,
                },
                timeout=30,
            )
            if response.ok:
                for entry in response.json().get("data", []):
                    values = entry.get("values") or [{}]
                    value = int(values[0].get("value", 0) or 0)
                    name = entry.get("name")
                    if name == "views":
                        views = value
                    elif name == "likes":
                        likes = value
                    elif name == "comments":
                        comments = value
                    elif name == "shares":
                        shares = value
                    elif name == "reach":
                        reach = value
                    elif name == "saved":
                        saved = value
                    elif name == "ig_reels_avg_watch_time":
                        # Reported in milliseconds.
                        watch_ms = value
        except requests.RequestException:
            pass

        if likes is None and comments is None:
            # Insights refused. Almost always the app missing
            # `instagram_manage_insights`, which is what carries reach and view
            # counts; the counters on the media itself need no such permission
            # and are better than recording nothing. Said out loud, because a
            # silent fallback looks like a platform that has no views.
            if not out:
                print("  -- instagram: no reach or views "
                      "(app lacks instagram_manage_insights); "
                      "using likes and comments")
            try:
                response = requests.get(
                    f"{base}/{media_id}",
                    params={"fields": "like_count,comments_count",
                            "access_token": token},
                    timeout=30,
                )
                if response.ok:
                    data = response.json()
                    likes = int(data.get("like_count", 0) or 0)
                    comments = int(data.get("comments_count", 0) or 0)
            except requests.RequestException:
                continue

        if views is None and likes is None:
            continue

        out[media_id] = _normalise(
            views=views, reach=reach, likes=likes or 0,
            comments=comments or 0, shares=shares or 0, saved=saved or 0,
            watch_seconds=(watch_ms / 1000.0) if watch_ms else None,
            clip_seconds=lengths.get(media_id),
        )

    return out


def _tiktok(cfg: Config, remote_ids: list[str]) -> dict[str, dict[str, Any]]:
    """
    Video stats through the Display API.

    Needs the `video.list` scope, which is separate from the publishing scopes
    and asked for in the same app review. Until that is granted this raises
    rather than returning nothing, so the reason shows up in the run log instead
    of looking like a platform with no posts.
    """
    token_path = cfg.publish.tiktok_token
    if not token_path.exists():
        raise RuntimeError(f"{token_path} not found — run 'shorts tiktok-auth'")

    try:
        token = json.loads(token_path.read_text(encoding="utf-8"))["access_token"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(f"{token_path} is unreadable") from exc

    response = requests.post(
        "https://open.tiktokapis.com/v2/video/query/",
        params={"fields": "id,view_count,like_count,comment_count,share_count"},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"filters": {"video_ids": list(remote_ids)}},
        timeout=30,
    )
    payload = response.json()
    error = (payload.get("error") or {}).get("code", "ok")
    if error not in ("ok", "", None):
        message = (payload.get("error") or {}).get("message", error)
        raise RuntimeError(f"{message} (scope video.list may not be granted)")

    out: dict[str, dict[str, Any]] = {}
    for video in (payload.get("data") or {}).get("videos", []):
        out[str(video.get("id"))] = _normalise(
            views=int(video.get("view_count", 0) or 0),
            likes=int(video.get("like_count", 0) or 0),
            comments=int(video.get("comment_count", 0) or 0),
            shares=int(video.get("share_count", 0) or 0),
        )
    return out


FETCHERS = {
    "youtube": _youtube,
    "instagram": _instagram,
    "tiktok": _tiktok,
}


def _clip_seconds(conn, remote_id: str) -> float | None:
    """How long the posted clip runs, for turning watch time into a percentage."""
    row = conn.execute(
        "SELECT c.start_s, c.end_s FROM posts p JOIN clips c ON c.id = p.clip_id "
        "WHERE p.remote_id = ?",
        (remote_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return float(row["end_s"]) - float(row["start_s"])
    except (TypeError, ValueError):
        return None


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
