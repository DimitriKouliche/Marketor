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
                    "SELECT clip_id FROM posts WHERE platform = ? "
                    "AND state IN ('posted', 'drafted')",
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

        copy: PostCopy = publish._copy_for(clip, cfg, platform)

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


def draft_to_tiktok(cfg: Config, *, clip_id: int | None = None) -> bool:
    """
    Uploads one clip to the account's TikTok drafts.

    The only publishing path TikTok leaves open to us: the audit that would
    allow direct posting was refused, and an unaudited app can only post
    SELF_ONLY, which is not publishing. Here the API delivers the file and a
    person posts it, which is precisely why it needs no audit.

    The clip is recorded as 'drafted', not 'posted'. It is sitting in an app
    waiting for someone to tap publish, and calling that a post would advance
    the rotation on a video nobody has seen and feed the strategy notes a
    history that did not happen.
    """
    from .publishers.tiktok import TikTokPublisher

    publisher = TikTokPublisher()
    ok, missing = publisher.available(cfg)
    if not ok:
        print(f"tiktok: {missing}")
        return False

    with db.connect(cfg.paths.database) as conn:
        if clip_id is not None:
            clip = conn.execute(
                "SELECT * FROM clips WHERE id = ?", (clip_id,)
            ).fetchone()
            if clip is None:
                print(f"clip {clip_id} not found")
                return False
        else:
            taken = {
                r["clip_id"] for r in conn.execute(
                    "SELECT clip_id FROM posts WHERE platform = 'tiktok' "
                    "AND state IN ('posted', 'drafted')"
                ).fetchall()
            }
            candidates = [
                c for c in db.clips_in_state(conn, "ready") if c["id"] not in taken
            ]
            if not candidates:
                print("tiktok: nothing ready that has not already been sent")
                return False
            clip = candidates[0]

        copy: PostCopy = publish._copy_for(clip, cfg, "tiktok")

        video = Path(clip["render_path"] or "")
        if not video.exists():
            print(f"clip {clip['id']}: {video} not found")
            return False

        print(f"  uploading clip {clip['id']} to TikTok drafts...")
        try:
            result = publisher.publish(cfg, video, copy)
        except Exception as exc:
            print(f"  !! tiktok clip {clip['id']}: {exc}")
            return False

        conn.execute(
            """
            INSERT INTO posts (clip_id, platform, state, variant, remote_id,
                               created_at)
            VALUES (?, 'tiktok', 'drafted', 'hashtags:manual', ?, ?)
            ON CONFLICT (clip_id, platform) DO UPDATE SET
                state = 'drafted', remote_id = excluded.remote_id
            """,
            (clip["id"], result.remote_id, db.now()),
        )
        conn.commit()

    print("")
    print("  In your TikTok drafts. Open the notification, then paste:")
    print("")
    print(f"  {copy.caption_with_tags}")
    print("")
    print(f"  Once posted:  shorts record {clip['id']} tiktok --url <permalink>")
    return True
