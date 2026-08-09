"""
The by-hand path.

TikTok and Instagram cannot post until someone else approves an app, and those
reviews take weeks. Rather than idle until then, the pipeline can stop one step
short: it picks the clip, writes the copy and hands you the file. You post it,
then tell it you did — and the cadence gate and the learning loop carry on as if
it had posted itself, which is the part that matters, because a platform the
system cannot see is one it plans badly around.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import db, publish
from .config import Config
from .publishers import PostCopy


def handoff(cfg: Config, platform: str, *, clip_id: int | None = None) -> int:
    """Prints everything needed to post one clip by hand. Returns its id, or 0."""
    if platform not in cfg.publish.platforms:
        print(f"{platform!r} is not in publish.platforms "
              f"({', '.join(cfg.publish.platforms) or 'none configured'})")
        return 0

    with db.connect(cfg.paths.database) as conn:
        if clip_id is not None:
            clip = conn.execute(
                "SELECT * FROM clips WHERE id = ?", (clip_id,)
            ).fetchone()
            if clip is None:
                print(f"clip {clip_id} not found")
                return 0
        else:
            ready, reason = publish.due(conn, cfg, platform)
            if not ready:
                print(f"{platform}: {reason}")
                print("  (pass a clip id to override)")
                return 0

            posted = {
                r["clip_id"] for r in conn.execute(
                    "SELECT clip_id FROM posts WHERE platform = ? AND state = 'posted'",
                    (platform,),
                ).fetchall()
            }
            candidates = [
                c for c in db.clips_in_state(conn, "ready") if c["id"] not in posted
            ]
            if not candidates:
                print(f"{platform}: nothing ready")
                return 0
            clip = candidates[0]

        copy: PostCopy = publish._copy_for(clip, platform)

    video = Path(clip["render_path"] or "")
    print(f"\n  Post this to {platform}")
    print(f"\n  file     {video}")
    if not video.exists():
        print("           !! missing; run 'shorts render'")
    print(f"\n  title    {copy.title}")
    print(f"  caption  {copy.caption}")
    if copy.hashtags:
        print(f"  tags     {' '.join(copy.hashtags)}")

    print(f"\n  Then:  shorts record {clip['id']} {platform} --url <permalink>")
    return int(clip["id"])


def record(
    cfg: Config,
    clip_id: int,
    platform: str,
    *,
    url: str = "",
    metric: int | None = None,
) -> bool:
    """
    Marks a hand-made post as posted.

    Without this the cadence gate never advances and `shorts learn` sees a
    platform that has never posted anything.
    """
    with db.connect(cfg.paths.database) as conn:
        if conn.execute("SELECT 1 FROM clips WHERE id = ?", (clip_id,)).fetchone() is None:
            print(f"clip {clip_id} not found")
            return False

        metrics = None
        if metric is not None:
            metrics = json.dumps({
                "views": None, "likes": 0, "comments": 0, "shares": 0,
                "primary": int(metric),
            })

        conn.execute(
            """
            INSERT INTO posts (clip_id, platform, state, variant, url,
                               metrics, posted_at, created_at)
            VALUES (?, ?, 'posted', 'hashtags:manual', ?, ?, ?, ?)
            ON CONFLICT (clip_id, platform) DO UPDATE SET
                state = 'posted',
                -- COALESCE throughout: re-running this to add a metric must not
                -- wipe the permalink recorded the first time.
                url = COALESCE(excluded.url, posts.url),
                metrics = COALESCE(excluded.metrics, posts.metrics),
                error = NULL,
                posted_at = COALESCE(posts.posted_at, excluded.posted_at)
            """,
            (clip_id, platform, url or None, metrics, db.now(), db.now()),
        )

    print(f"  recorded clip {clip_id} as posted to {platform}")
    if metric is None:
        print(f"  add performance later:  shorts record {clip_id} {platform} --metric <n>")
    return True
