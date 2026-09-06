"""
Publishing queue.

Walks approved clips and posts them to each configured platform. The uniqueness
constraint on (clip, platform) in the database is what guarantees a clip is
never posted twice, even if this is interrupted halfway through.
"""

from __future__ import annotations

import json
import re
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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


def dimensions_of(clip) -> dict[str, Any]:
    """
    The attributes of a clip worth measuring performance against.

    Answering "does endless beat levels", "is hardcore worth recording", "which
    power-ups make a better clip" needs each post to carry what its footage was.
    None of it can be recovered later: the clip is rewritten on every
    re-selection, and the recording it came from is eventually archived.
    """
    try:
        context = json.loads(clip["context"] or "{}")
    except (json.JSONDecodeError, TypeError):
        context = {}

    scene = str(context.get("scene") or "")
    level = context.get("level") or {}
    events = context.get("nearby_events") or []


    return {
        "mode": "endless" if "endless" in scene.lower() else "level",
        "scene": scene,
        # Null in endless: the setting exists and changes nothing there, so
        # recording it would invite an analysis of a variable with no effect.
        "difficulty": (
            None if "endless" in scene.lower() else level.get("difficulty")
        ),
        "trigger": context.get("trigger"),
        "seconds": round(float(clip["end_s"]) - float(clip["start_s"]), 1),
        "peak_speed": context.get("peak_speed"),
        "mean_speed": context.get("mean_speed"),
        "deaths": context.get("deaths_in_window"),
        "wall_bursts": context.get("wall_bursts"),
        "projectiles_dodged": context.get("projectiles_dodged"),
        "hazards_on_screen": context.get("hazards_on_screen"),
        "diamonds": sum(
            1 for e in events
            if e.get("name") in ("bonus_pickup", "bonus_secured", "bonus_lost")
        ),
        # Split, because the two are not the same decision. Every checkpoint
        # forces one bonus and one malus, so the presence of maluses measures
        # how far the run got, not a taste for danger. What varies, and what is
        # worth measuring, is *which* of each was taken.
        "bonuses": {
            name: level for name, level in (context.get("powerup_build") or {}).items()
            if not name.lower().startswith("malus")
        },
        "maluses": {
            name: level for name, level in (context.get("powerup_build") or {}).items()
            if name.lower().startswith("malus")
        },
        # One bonus and one malus per checkpoint, so this is how many the run
        # had reached — a plain proxy for progression, and the thing every
        # upgrade count is confounded with.
        "checkpoints_reached": sum(
            level for name, level in (context.get("powerup_build") or {}).items()
            if not name.lower().startswith("malus")
        ),
        "attempt": level.get("attempt_number_on_this_level"),
    }


def _searchable(text: str) -> str:
    """The caption with hashtags removed, so a tag cannot pass for a mention."""
    return re.sub(r"#\w+", " ", text or "").lower()


def _name_the_game(caption: str, brand: str) -> str:
    """
    Works the game's name into a caption that never mentions it.

    Ahead of the trailing hashtags rather than after them: a caption ends with
    its tags, and a name tacked on behind them reads as an afterthought and
    sits below where anyone stops reading.
    """
    words = (caption or "").split()
    tail = []
    while words and words[-1].startswith("#"):
        tail.insert(0, words.pop())

    prose = " ".join(words).rstrip()
    if prose and prose[-1] not in ".!?":
        prose += "."
    prose = f"{prose} {brand}." if prose else f"{brand}."
    return " ".join([prose] + tail) if tail else prose


def _copy_for(clip, cfg: Config, platform: str) -> PostCopy:
    """Rebuilds the per-platform copy stored at the write stage."""
    try:
        captions = json.loads(clip["caption"] or "{}")
    except json.JSONDecodeError:
        captions = {}
    try:
        extra = json.loads(clip["hashtags"] or "{}")
    except json.JSONDecodeError:
        extra = {}

    caption = captions.get(platform) or clip["hook"] or ""

    # Last resort, checked here because this is the last place before the text
    # leaves the machine. Measured before the writer was told to do it, 81% of
    # Instagram captions and 89% of TikTok ones said "our own game" and never
    # named it — which sends anyone who liked the clip to a search box with
    # nothing to type. The writer and the editor both handle this now; this
    # only catches what both missed.
    brand = cfg.copy.brand
    if brand and brand.lower() not in _searchable(caption):
        caption = _name_the_game(caption, brand)

    return PostCopy(
        title=extra.get("youtube_title") or clip["hook"] or brand,
        caption=caption,
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


def promo_slot(cfg: Config, platform: str, day: date) -> int | None:
    """
    The hour this platform posts on a given day during a promotion, or None.

    Drawn from the date and the platform name rather than at random each run, so
    every run of the day agrees on the answer without storing it anywhere — and
    so the two accounts land at different hours instead of together, which is
    both better for a feed and closer to what a person would do.
    """
    promo = cfg.publish.promo
    if not promo or not promo.get("until"):
        return None

    try:
        until = date.fromisoformat(str(promo["until"]))
    except ValueError:
        return None
    if day > until:
        return None

    first = int(promo.get("from_hour", 16))
    last = int(promo.get("to_hour", 22))
    if last < first:
        return None
    return random.Random(f"{platform}:{day.isoformat()}").randint(first, last)


def posted_today(conn, cfg: Config, platform: str) -> int:
    """How many times this platform has posted since local midnight."""
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COUNT(*) n FROM posts WHERE platform = ? AND state = 'posted' "
        "AND posted_at >= ? AND COALESCE(catch_up, 0) = 0",
        (platform, midnight.astimezone(timezone.utc).isoformat()),
    ).fetchone()
    return row["n"] if row else 0


def _promo_due(conn, cfg: Config, platform: str) -> tuple[bool, str] | None:
    """The promotion's own gate, or None when no promotion is running."""
    now = datetime.now().astimezone()
    slot = promo_slot(cfg, platform, now.date())
    if slot is None:
        return None

    # Local midnight, because the window is expressed in local hours and a
    # promotion that resets at 01:00 local would post twice in one evening.
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COUNT(*) n FROM posts WHERE platform = ? AND state = 'posted' "
        "AND posted_at >= ? AND COALESCE(catch_up, 0) = 0",
        (platform, midnight.astimezone(timezone.utc).isoformat()),
    ).fetchone()
    if row and row["n"] >= int(cfg.publish.promo.get("per_day", 1)):
        return False, f"promo: already posted today (slot was {slot:02d}h)"

    if now.hour < slot:
        return False, f"promo: today's slot is {slot:02d}h"

    return True, ""


def due(conn, cfg: Config, platform: str) -> tuple[bool, str]:
    """
    Whether enough time has passed since this platform's last post.

    Cadence is what turns a backlog of ready clips into a steady drip. Without
    it a single run empties the queue onto every platform at once, which is both
    the worst thing for reach and the most obvious automation signal.
    """
    # A promotion replaces the cadence outright rather than shortening it: the
    # two rules answer different questions, and running both means the slower
    # one silently wins.
    promo = _promo_due(conn, cfg, platform)
    if promo is not None:
        return promo

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
    catch_up: bool = False,
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

        # Same footage under a different clip id. Happens after `regen --full`,
        # which rebuilds the database and renumbers everything — the row-level
        # check above cannot see across that, the fingerprint can.
        if clip["fingerprint"]:
            twin = conn.execute(
                """
                SELECT p.clip_id, p.url FROM posts p
                  JOIN clips c ON c.id = p.clip_id
                 WHERE c.fingerprint = ? AND p.platform = ?
                   AND p.state = 'posted' AND p.clip_id != ?
                """,
                (clip["fingerprint"], platform, clip_id),
            ).fetchone()
            if twin:
                print(f"  -- meme sequence deja publiee sur {platform} "
                      f"(clip {twin['clip_id']}) : {twin['url'] or ''}")
                return False

        try:
            publisher = get_publisher(platform)
        except PublishError as exc:
            print(f"  !! {platform}: {exc}")
            return False

        copy = _copy_for(clip, cfg, platform)

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
                             error = NULL, posted_at = ?, dimensions = ?,
                             catch_up = ?
             WHERE clip_id = ? AND platform = ?
            """,
            (result.remote_id, result.url, db.now(),
             json.dumps(dimensions_of(clip)), 1 if catch_up else 0,
             clip_id, platform),
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
                copy = _copy_for(clip, cfg, platform)

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
                           error = NULL, posted_at = ?, dimensions = ?
                     WHERE clip_id = ? AND platform = ?
                    """,
                    (result.remote_id, result.url, db.now(),
                     json.dumps(dimensions_of(clip)), clip["id"], platform),
                )
                conn.commit()

                posted += 1
                budget -= 1
                if verbose:
                    print(f"  + {platform} clip {clip['id']}: {result.url or result.remote_id}")

    return posted
