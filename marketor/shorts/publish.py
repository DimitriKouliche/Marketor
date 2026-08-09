"""
Publishing queue.

Walks approved clips and posts them to each configured platform. The uniqueness
constraint on (clip, platform) in the database is what guarantees a clip is
never posted twice, even if this is interrupted halfway through.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import Config
from .publishers import PostCopy, PublishError, get_publisher


def check(cfg: Config) -> dict[str, tuple[bool, str]]:
    """Reports which platforms are actually ready to post."""
    status: dict[str, tuple[bool, str]] = {}
    for platform in cfg.publish.platforms:
        try:
            status[platform] = get_publisher(platform).available(cfg)
        except PublishError as exc:
            status[platform] = (False, str(exc))
    return status


def _copy_for(clip, platform: str) -> PostCopy:
    """Rebuilds the per-platform copy stored at the write stage."""
    try:
        captions = json.loads(clip["caption"] or "{}")
    except json.JSONDecodeError:
        captions = {}
    try:
        extra = json.loads(clip["hashtags"] or "{}")
    except json.JSONDecodeError:
        extra = {}

    return PostCopy(
        title=extra.get("youtube_title") or clip["hook"] or "This is no cave",
        caption=captions.get(platform) or clip["hook"] or "",
        hashtags=(extra.get("hashtags") or {}).get(platform, []),
    )


def assign_variant(conn, cfg: Config, platform: str, clip) -> tuple[str, list[str]]:
    """
    Picks this post's experiment arm and returns (variant label, hashtags).

    Round-robin on the count of posts already made to the platform, so arms stay
    balanced from the very first handful of posts. Random assignment needs
    hundreds of samples to even out, which at two posts a day is months.
    """
    sets = (cfg.experiments.hashtag_variants or {}).get(platform) or {}
    if not cfg.experiments.enabled or not sets:
        stored = {}
        try:
            stored = json.loads(clip["hashtags"] or "{}").get("hashtags") or {}
        except json.JSONDecodeError:
            pass
        return "default", stored.get(platform, [])

    names = sorted(sets)
    row = conn.execute(
        "SELECT COUNT(*) n FROM posts WHERE platform = ?", (platform,)
    ).fetchone()
    name = names[(row["n"] if row else 0) % len(names)]

    return f"hashtags:{name}", list(sets[name])


def due(conn, cfg: Config, platform: str) -> tuple[bool, str]:
    """
    Whether enough time has passed since this platform's last post.

    Cadence is what turns a backlog of ready clips into a steady drip. Without
    it a single run empties the queue onto every platform at once, which is both
    the worst thing for reach and the most obvious automation signal.
    """
    days = float(cfg.publish.cadence_days.get(platform, 0))
    if days <= 0:
        return True, ""

    row = conn.execute(
        "SELECT MAX(posted_at) last FROM posts WHERE platform = ? AND state = 'posted'",
        (platform,),
    ).fetchone()
    if not row or not row["last"]:
        return True, ""

    try:
        last = datetime.fromisoformat(row["last"])
    except ValueError:
        return True, ""

    elapsed = datetime.now(timezone.utc) - last
    if elapsed >= timedelta(days=days):
        return True, ""

    remaining = timedelta(days=days) - elapsed
    hours = remaining.total_seconds() / 3600
    return False, f"next post in {hours:.0f}h (every {days:g} day(s))"


def _posted_today(conn, platform: str) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) n FROM posts WHERE platform = ? AND state = 'posted' "
        "AND posted_at >= ?",
        (platform, since),
    ).fetchone()
    return row["n"] if row else 0


def _arm_name(cfg: Config, platform: str, hashtags: list[str]) -> str:
    """Names the experiment arm a set of hashtags belongs to."""
    sets = (cfg.experiments.hashtag_variants or {}).get(platform) or {}
    chosen = {h.lower().lstrip("#") for h in hashtags}
    for name, tags in sets.items():
        if {t.lower().lstrip("#") for t in tags} == chosen:
            return name
    return "planner"


def publish_one(
    cfg: Config,
    clip_id: int,
    platform: str,
    *,
    hashtags: list[str] | None = None,
    dry_run: bool = False,
) -> bool:
    """
    Posts one specific clip to one specific platform.

    The orchestrated counterpart to publish_ready, which picks for itself. The
    planner has already chosen, so this only carries out the instruction — but
    the (clip, platform) uniqueness constraint still applies, so a replayed or
    duplicated plan cannot double-post.
    """
    with db.connect(cfg.paths.database) as conn:
        clip = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if clip is None:
            print(f"  !! clip {clip_id} not found")
            return False

        already = conn.execute(
            "SELECT state FROM posts WHERE clip_id = ? AND platform = ?",
            (clip_id, platform),
        ).fetchone()
        if already and already["state"] == "posted":
            print(f"  -- clip {clip_id} already posted to {platform}")
            return False

        try:
            publisher = get_publisher(platform)
        except PublishError as exc:
            print(f"  !! {platform}: {exc}")
            return False

        copy = _copy_for(clip, platform)

        # Whatever the planner chose still has to be labelled as an experiment
        # arm, or `shorts learn` cannot tell its posts apart. The planner is
        # shown the configured hashtag sets and asked to keep the test running,
        # so a set it echoes back must land in that set's bucket rather than
        # starting a new one.
        variant, rotated = assign_variant(conn, cfg, platform, clip)
        if hashtags:
            copy.hashtags = hashtags
            variant = f"hashtags:{_arm_name(cfg, platform, hashtags)}"
        else:
            copy.hashtags = rotated

        if dry_run:
            print(f"  [dry-run] {platform} <- clip {clip_id}: {copy.title}")
            if copy.hashtags:
                print(f"            tags: {' '.join(copy.hashtags)}")
            return True

        conn.execute(
            """
            INSERT INTO posts (clip_id, platform, state, variant, created_at)
            VALUES (?, ?, 'queued', ?, ?)
            ON CONFLICT (clip_id, platform)
            DO UPDATE SET state = 'queued', variant = excluded.variant
            """,
            (clip_id, platform, variant, db.now()),
        )
        conn.commit()

        try:
            result = publisher.publish(cfg, Path(clip["render_path"] or ""), copy)
        except Exception as exc:
            conn.execute(
                "UPDATE posts SET state = 'failed', error = ? "
                "WHERE clip_id = ? AND platform = ?",
                (str(exc)[:2000], clip_id, platform),
            )
            conn.commit()
            print(f"  !! {platform} clip {clip_id}: {exc}")
            return False

        conn.execute(
            """
            UPDATE posts SET state = 'posted', remote_id = ?, url = ?,
                             error = NULL, posted_at = ?
             WHERE clip_id = ? AND platform = ?
            """,
            (result.remote_id, result.url, db.now(), clip_id, platform),
        )
        conn.commit()
        print(f"  + {platform} clip {clip_id}: {result.url or result.remote_id}")
        return True


def publish_ready(cfg: Config, *, dry_run: bool = False, verbose: bool = True) -> int:
    """Posts approved clips. Returns the number of successful posts."""
    posted = 0

    with db.connect(cfg.paths.database) as conn:
        clips = db.clips_in_state(conn, "approved")
        if not clips:
            if verbose:
                print("No approved clips. Use 'shorts approve <id>' first.")
            return 0

        for platform in cfg.publish.platforms:
            try:
                publisher = get_publisher(platform)
            except PublishError as exc:
                print(f"  !! {platform}: {exc}")
                continue

            ready, reason = publisher.available(cfg)
            if not ready:
                print(f"  -- {platform} not configured: {reason}")
                continue

            ready, reason = due(conn, cfg, platform)
            if not ready and not dry_run:
                print(f"  -- {platform}: {reason}")
                continue

            budget = cfg.publish.max_posts_per_platform_per_day - _posted_today(conn, platform)
            if budget <= 0:
                print(f"  -- {platform}: daily cap reached")
                continue

            eligible = list(clips)

            # One post per cadence window, not a batch.
            budget = min(budget, 1)

            for clip in eligible:
                if budget <= 0:
                    break

                existing = conn.execute(
                    "SELECT state FROM posts WHERE clip_id = ? AND platform = ?",
                    (clip["id"], platform),
                ).fetchone()
                if existing and existing["state"] == "posted":
                    continue

                video = Path(clip["render_path"] or "")
                copy = _copy_for(clip, platform)

                variant, hashtags = assign_variant(conn, cfg, platform, clip)
                copy.hashtags = hashtags

                if dry_run:
                    tags = " ".join(hashtags) or "(none)"
                    print(
                        f"  [dry-run] {platform} <- clip {clip['id']} "
                        f"[{variant}]: {copy.title}"
                    )
                    print(f"            tags: {tags}")
                    budget -= 1
                    continue

                conn.execute(
                    """
                    INSERT INTO posts (clip_id, platform, state, variant, created_at)
                    VALUES (?, ?, 'queued', ?, ?)
                    ON CONFLICT (clip_id, platform)
                    DO UPDATE SET state = 'queued', variant = excluded.variant
                    """,
                    (clip["id"], platform, variant, db.now()),
                )
                conn.commit()

                try:
                    result = publisher.publish(cfg, video, copy)
                except Exception as exc:
                    # Deliberately broad: a network blip on one platform must
                    # not abandon the rest of the queue.
                    conn.execute(
                        "UPDATE posts SET state = 'failed', error = ? "
                        "WHERE clip_id = ? AND platform = ?",
                        (str(exc)[:2000], clip["id"], platform),
                    )
                    conn.commit()
                    print(f"  !! {platform} clip {clip['id']}: {exc}")
                    continue

                conn.execute(
                    """
                    UPDATE posts
                       SET state = 'posted', remote_id = ?, url = ?,
                           error = NULL, posted_at = ?
                     WHERE clip_id = ? AND platform = ?
                    """,
                    (result.remote_id, result.url, db.now(), clip["id"], platform),
                )
                conn.commit()

                posted += 1
                budget -= 1
                if verbose:
                    print(f"  + {platform} clip {clip['id']}: {result.url or result.remote_id}")

    return posted
