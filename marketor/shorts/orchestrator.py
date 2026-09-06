"""
The planner.

Decides what to post today, where, and with what angle — then hands each
decision to the writer as a brief. Runs on the stronger model because its
mistakes compound: a bad caption costs one post, a bad posting strategy costs
weeks of reach and can get an account banned.

The important structural choice is that **the plan is a proposal**. Everything
the model returns is checked against the same constraints the manual path
enforces: cadence windows, platform readiness, and which clips actually exist.
A system meant to run unattended for months cannot be allowed to reason its way
past its own rate limits, so it does not get the chance — the code decides what is legal, the model only decides
what is good.

State in, plan out, notes carried forward. That last part is what makes months
of separate invocations behave like one continuous strategy instead of forty
unrelated cold starts.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db, env, learn, publish
from .config import Config
from .copywriter import PROVIDERS

MEMORY_HEADER = """# Marketing strategy notes

Written and rewritten by the orchestrator on every run. Edit freely — it reads
this back each time and treats it as standing direction.
"""

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "situation": {
            "type": "string",
            "description": (
                "Two or three sentences on what the data shows right now and "
                "what you are therefore doing. Written for the operator "
                "reviewing this later."
            ),
        },
        "posts": {
            "type": "array",
            "description": (
                "What to post this run. May be empty if nothing is due or "
                "nothing available is good enough — posting weak material to "
                "fill a slot is worse than skipping it."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string"},
                    "clip_id": {"type": "integer"},
                    "hashtags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "brief": {
                        "type": "string",
                        "description": (
                            "Direction for the writer: the angle to take, what "
                            "to emphasise, what to avoid. One or two sentences. "
                            "Do not write the caption yourself — the writer "
                            "does that from the clip's telemetry."
                        ),
                    },
                    "why": {
                        "type": "string",
                        "description": "Why this clip, platform and angle.",
                    },
                },
                "required": ["platform", "clip_id", "hashtags", "brief", "why"],
                "additionalProperties": False,
            },
        },
        "strategy_notes": {
            "type": "string",
            "description": (
                "The full replacement for your notes file. You will read this "
                "back next run and nothing else carries over, so record what "
                "you have learned, what you are testing and what you expect. "
                "Keep it under 400 words; prune anything stale."
            ),
        },
        # An array rather than an event-name-to-number map: OpenAI's strict
        # mode requires every object key to be declared up front, so open
        # dictionaries are not expressible.
        "weight_changes": {
            "type": "array",
            "description": (
                "Optional selection-weight adjustments. Only propose these "
                "with real evidence; leave the array empty otherwise."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "event": {"type": "string"},
                    "weight": {"type": "number"},
                },
                "required": ["event", "weight"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["situation", "posts", "strategy_notes", "weight_changes"],
    "additionalProperties": False,
}


def load_memory(cfg: Config) -> str:
    path = Path(cfg.orchestrator.memory_file)
    if not path.exists():
        return "(no notes yet — this is the first run)"
    return path.read_text(encoding="utf-8")


def save_memory(cfg: Config, notes: str) -> Path:
    path = Path(cfg.orchestrator.memory_file)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    path.write_text(f"{MEMORY_HEADER}\n_Last updated {stamp}_\n\n{notes.strip()}\n",
                    encoding="utf-8")
    return path


def _mode(clip) -> str:
    """
    Endless or a built level, for a clip.

    Endless is where the worm and the missiles are, so it produces livelier
    footage per minute and scores higher — measured, it was a third of the play
    time and half the top of the ranking. That is the scorer being right, not
    wrong: more genuinely happens there. It only becomes a problem one step
    later, when every post in a row comes from the same mode and the feed stops
    showing the game people would actually buy.
    """
    try:
        scene = (json.loads(clip["context"] or "{}") or {}).get("scene") or ""
    except (json.JSONDecodeError, TypeError):
        return "level"
    return "endless" if "endless" in str(scene).lower() else "level"


def gather(
    cfg: Config, *, force: bool = False, only: str | None = None
) -> dict[str, Any]:
    """Everything the planner needs to decide, and nothing it doesn't."""
    now = datetime.now(timezone.utc)
    state: dict[str, Any] = {
        "today": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
    }

    # Platforms the planner is actually allowed to use. A clip that has been
    # everywhere it can go is finished, and listing it again only invites the
    # planner to waste a slot on a post that will be refused.
    plannable = [
        p for p in cfg.publish.platforms
        if p not in cfg.orchestrator.manual_platforms
    ]

    with db.connect(cfg.paths.database) as conn:
        posted_to: dict[int, set[str]] = {}
        for row in conn.execute(
            "SELECT clip_id, platform FROM posts WHERE state = 'posted'"
        ):
            posted_to.setdefault(row["clip_id"], set()).add(row["platform"])

        # A clip's score is only comparable to others cut from the same
        # recording. The event log gained new signals over time — bursts,
        # wavebursts, gas — so a session recorded after that change scores
        # higher than an identical one recorded before it, for reasons that
        # have nothing to do with the footage. Left raw, every older recording
        # would sink below every newer one and quietly stop being posted.
        #
        # So each clip also carries where it stands inside its own recording,
        # which is the comparison that survives the log changing under it.
        ready = list(db.clips_in_state(conn, "ready"))
        by_source: dict[Any, list[float]] = {}
        for clip in ready:
            by_source.setdefault(clip["source_id"], []).append(float(clip["score"]))
        for scores in by_source.values():
            scores.sort()

        def standing(clip) -> int:
            scores = by_source.get(clip["source_id"]) or []
            if len(scores) < 2:
                return 100
            below = sum(1 for s in scores if s < float(clip["score"]))
            return round(100 * below / (len(scores) - 1))

        # Only clips that could legally be posted.
        state["available_clips"] = [
            {
                "clip_id": c["id"],
                "trigger": c["trigger_event"],
                "mode": _mode(c),
                "score": round(float(c["score"]), 1),
                # 0-100, against the other clips from the same recording.
                "standing": standing(c),
                "seconds": round(float(c["end_s"]) - float(c["start_s"]), 1),
                "hook": c["hook"],
                # Where this clip has already run. The rotation is the planner's
                # to drive, so it has to be able to see it.
                "posted_to": sorted(posted_to.get(c["id"], ())),
                "remaining_platforms": sorted(
                    set(plannable) - posted_to.get(c["id"], set())
                ),
            }
            for c in ready
            if set(plannable) - posted_to.get(c["id"], set())
        ]

        # Which platforms are actually postable right now, and why not.
        # Platforms reserved for manual posting are not mentioned at all — a
        # planner that knows about a platform it may not use will keep
        # proposing it.
        platforms = {}
        forced: set[str] = set()
        for platform in cfg.publish.platforms:
            if platform in cfg.orchestrator.manual_platforms:
                continue
            # Narrowing the run to one platform, so that catching up a missed
            # slot on one account cannot spend another account's slot for the
            # day. Omitted entirely rather than marked not-due: a planner told
            # about a platform it may not use keeps proposing it.
            if only and platform != only:
                continue
            ready, reason = publish.due(conn, cfg, platform)
            if force and not ready:
                # Catching up a missed slot, never granting a second one. A
                # platform that has already posted today keeps its gate: the
                # point of the override is the slot that did not happen, and
                # forcing a platform that already went out just burns a
                # rotation slot on a day it did not need one.
                already = publish.posted_today(conn, cfg, platform)
                if already:
                    reason = f"{reason} (deja publie aujourd'hui)".strip()
                else:
                    ready, reason = True, ""
                    forced.add(platform)

            configured, missing = True, ""
            try:
                from .publishers import get_publisher

                configured, missing = get_publisher(platform).available(cfg)
            except Exception as exc:
                configured, missing = False, str(exc)

            platforms[platform] = {
                "due": ready,
                "catch_up": platform in forced,
                "cadence_days": cfg.publish.cadence_days.get(platform),
                "blocked_by": (reason or None) if not ready else None,
                "credentials_ok": configured,
                "credential_problem": missing or None,
            }
        state["platforms"] = platforms

        # Recent history, so it does not repeat itself.
        rows = conn.execute(
            """
            SELECT p.platform, p.variant, p.posted_at, p.metrics, p.url,
                   p.clip_id, c.trigger_event, c.hook,
                   CASE WHEN lower(c.context) LIKE '%endless%'
                        THEN 'endless' ELSE 'level' END AS mode_scene
              FROM posts p JOIN clips c ON c.id = p.clip_id
             WHERE p.state = 'posted'
             ORDER BY p.posted_at DESC LIMIT ?
            """,
            (cfg.orchestrator.history_limit,),
        ).fetchall()

        history = []
        for row in rows:
            try:
                metrics = json.loads(row["metrics"] or "{}")
            except json.JSONDecodeError:
                metrics = {}
            history.append({
                "platform": row["platform"],
                "clip_id": row["clip_id"],
                "mode": row["mode_scene"],
                "posted_at": row["posted_at"],
                "variant": row["variant"],
                "trigger": row["trigger_event"],
                "hook": row["hook"],
                "performance": metrics.get("primary"),
            })
        state["recent_posts"] = history

    # Aggregated performance, reusing the learning report's own bucketing so the
    # planner and `shorts learn` never disagree about what the data says.
    data = learn._collect(cfg)
    state["measured_posts"] = data["measured"]
    state["performance"] = {
        "by_hashtag_set": {
            f"{p}/{label}": {"mean": round(g.mean, 1), "n": g.n}
            for (p, label), g in data["by_variant"].items()
        },
        "by_clip_type": {
            name: {"mean": round(g.mean, 1), "n": g.n}
            for name, g in data["by_trigger"].items()
        },
        "min_samples_before_trusting": cfg.experiments.min_samples,
    }

    state["hashtag_sets"] = {
        p: sets for p, sets in (cfg.experiments.hashtag_variants or {}).items()
        if p in platforms
    }
    state["selection_weights"] = cfg.selection.weights
    return state


def _system_prompt(cfg: Config) -> str:
    return (
        "You plan the social posting for a finished indie game. You decide what "
        "goes out, where, and with what angle. A separate writer produces the "
        "actual captions from each clip's gameplay telemetry — you give it "
        "direction, never finished copy.\n\n"
        "You run unattended, on a schedule, for months. Nobody reviews your "
        "reasoning between runs. Two consequences:\n"
        "- Your notes are your only memory. Anything not written into "
        "strategy_notes is forgotten before the next run.\n"
        "- A mistake persists. Prefer the boring correct choice over the "
        "interesting risky one.\n\n"
        "HARD RULES. These are enforced in code after you answer, so breaking "
        "them wastes the run rather than achieving anything:\n"
        "- Only post to a platform whose `due` is true and `credentials_ok` is "
        "true.\n"
        "- At most one post per platform per run.\n"
        "- `clip_id` must come from available_clips.\n"
        "- Never post a clip to a platform listed in its `posted_to`.\n"
        "- Never send the same clip to two platforms in the same run.\n\n"
        "ROTATION. Every clip is meant to reach every platform "
        "eventually, one platform at a time. The catalogue is finite, and "
        "a clip already posted somewhere is not used up — it is part-way "
        "through, and finishing it costs nothing from the pool. Prefer "
        "clips with a short `remaining_platforms` over untouched ones of "
        "similar quality, and keep the platforms on different clips so "
        "the accounts do not read as one feed mirrored three times.\n\n"
        "JUDGEMENT, which is what you are actually for:\n"
        "- Do not fill a slot with weak material. An empty posts array is a "
        "valid, and often correct, answer. Score is a strong signal: anything "
        "much below the others in available_clips is probably not worth a slot. "
        "Compare `standing` rather than `score` across two different "
        "recordings: score is only meaningful against clips cut from the "
        "same session, because what the game logged changed over time.\n"
        "- Vary. Repeating a clip type or an angle across consecutive posts is "
        "the main way an automated account becomes obvious. Check recent_posts "
        "before choosing. `mode` matters as much as `trigger` here: endless "
        "runs score higher because more happens in them, so left alone they "
        "fill the feed and it stops showing the built levels the game is "
        "actually sold on. Alternate between the two.\n"
        "- Weigh the day of the week: weekend traffic differs from weekday.\n"
        "- Use the performance data, but only where n is at or above "
        "min_samples_before_trusting. Below that it is noise, and acting on it "
        "is worse than ignoring it. Say so in your notes rather than pretending "
        "to a conclusion.\n"
        "- Keep experiments running. If one hashtag set is ahead but both are "
        "still under the sample threshold, keep alternating rather than "
        "switching early and ending the test.\n\n"
        "You do not have live trend data. Do not claim to know what is "
        "currently trending, invent a hashtag because it sounds popular, or "
        "reference events you cannot verify. Work from the performance numbers, "
        "the calendar, and your own notes."
    )


def _user_prompt(state: dict[str, Any], memory: str) -> str:
    return (
        f"Your notes from previous runs:\n\n{memory}\n\n"
        f"Current state:\n\n{json.dumps(state, indent=2, default=str)}"
    )


def plan(
    cfg: Config, *, verbose: bool = True, force: bool = False,
    only: str | None = None,
) -> dict[str, Any] | None:
    """Asks the planner what to do. Returns the validated plan, or None."""
    env.ensure_loaded()

    preset = PROVIDERS.get(cfg.orchestrator.provider, PROVIDERS["openai"])
    api_key = os.environ.get(preset["api_key_env"])
    if not api_key:
        print(f"  !! {preset['api_key_env']} is not set; cannot plan")
        return None

    try:
        from openai import OpenAI
    except ImportError:
        print("  !! openai package not installed")
        return None

    client = OpenAI(
        api_key=api_key,
        base_url=preset["base_url"],
        # Bounded, because this runs unattended behind a lock. A request
        # that never returns freezes the pipeline, and the lock then
        # blocks every scheduled run behind it — measured once, a hung
        # call held the lock for two hours and cost the day's posts.
        timeout=cfg.orchestrator.request_timeout,
        max_retries=2,
    )
    state = gather(cfg, force=force, only=only)
    memory = load_memory(cfg)

    if verbose:
        due = [p for p, v in state["platforms"].items() if v["due"] and v["credentials_ok"]]
        print(f"  {len(state['available_clips'])} clip(s) ready, "
              f"{state['measured_posts']} measured post(s)")
        print(f"  platforms due now: {', '.join(due) or 'none'}")

    kwargs: dict[str, Any] = {
        "model": cfg.orchestrator.model,
        "max_completion_tokens": 6000,
        "messages": [
            {"role": "system", "content": _system_prompt(cfg)},
            {"role": "user", "content": _user_prompt(state, memory)},
        ],
    }
    if preset["schema_mode"] == "json_schema":
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "plan", "schema": _PLAN_SCHEMA, "strict": True},
        }
    else:
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"][0]["content"] += (
            "\n\nReply with a single json object matching this schema:\n"
            + json.dumps(_PLAN_SCHEMA, indent=2)
        )

    try:
        response = client.chat.completions.create(**kwargs)
        raw = response.choices[0].message.content or "{}"
        proposed = json.loads(raw)
    except Exception as exc:
        print(f"  !! planning failed: {exc}")
        return None

    return _enforce(cfg, state, proposed, verbose=verbose)


def _enforce(
    cfg: Config, state: dict[str, Any], proposed: dict[str, Any], *, verbose: bool
) -> dict[str, Any]:
    """
    Drops anything in the plan that breaks a hard rule.

    The planner is told these rules, but being told is not the same as being
    prevented. This is the part that makes unattended operation safe: the worst
    a confused plan can do is get discarded.
    """
    clips = {c["clip_id"]: c for c in state["available_clips"]}
    approved: list[dict[str, Any]] = []
    rejected: list[str] = []
    used_platforms: set[str] = set()
    used_clips: set[int] = set()

    for post in proposed.get("posts") or []:
        platform = str(post.get("platform", ""))
        clip_id = post.get("clip_id")
        info = state["platforms"].get(platform)

        if platform in cfg.orchestrator.manual_platforms:
            rejected.append(f"{platform}: reserved for manual posting")
            continue
        if info is None:
            rejected.append(f"{platform}: not a configured platform")
            continue
        if platform in used_platforms:
            rejected.append(f"{platform}: more than one post proposed this run")
            continue
        if not info["due"]:
            rejected.append(f"{platform}: not due ({info['blocked_by']})")
            continue
        if not info["credentials_ok"]:
            rejected.append(f"{platform}: {info['credential_problem']}")
            continue

        clip = clips.get(clip_id)
        if clip is None:
            rejected.append(f"{platform}: clip {clip_id} is not available")
            continue

        # The rotation, enforced rather than requested. Every clip is meant to
        # reach every platform eventually, which only works if no platform gets
        # the same clip twice and no two platforms get it on the same day.
        if platform in (clip.get("posted_to") or []):
            rejected.append(f"{platform}: clip {clip_id} already ran there")
            continue
        if clip_id in used_clips:
            rejected.append(
                f"{platform}: clip {clip_id} is already going out this run"
            )
            continue

        used_platforms.add(platform)
        used_clips.add(clip_id)
        # Carried from the gate, not from the planner: whether this post is a
        # catch-up is a fact about the schedule, not a decision the model makes.
        post["catch_up"] = bool(info.get("catch_up"))
        approved.append(post)

    if verbose:
        for line in rejected:
            print(f"  -- dropped {line}")

    return {
        "situation": proposed.get("situation", ""),
        "posts": approved,
        "rejected": rejected,
        "strategy_notes": proposed.get("strategy_notes", ""),
        "weight_changes": _weight_map(proposed.get("weight_changes")),
    }


def _weight_map(raw: Any) -> dict[str, float]:
    """Accepts either the array shape or a plain mapping, returns a mapping."""
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items()}

    changes: dict[str, float] = {}
    for item in raw or []:
        if isinstance(item, dict) and "event" in item and "weight" in item:
            try:
                changes[str(item["event"])] = float(item["weight"])
            except (TypeError, ValueError):
                continue
    return changes


def execute(cfg: Config, plan_result: dict[str, Any], *, dry_run: bool = False) -> int:
    """
    Carries out an approved plan: rewrite the copy to the brief, then post.

    The copy is regenerated per post rather than reused, because the planner's
    brief is the whole point — a clip sent out with a "lean on the tuning
    story" direction should not carry the words it was written with before.

    Note this path posts clips in the 'ready' state, skipping the manual
    'approved' gate that `shorts publish` waits on. That gate is a person, and
    the whole premise here is that there isn't one. QC still applies: a clip
    only reaches 'ready' by passing it.
    """
    from . import copywriter

    posted = 0

    for post in plan_result["posts"]:
        platform = post["platform"]
        clip_id = int(post["clip_id"])
        brief = str(post.get("brief") or "")
        hashtags = [str(h) for h in (post.get("hashtags") or [])]

        print(f"\n  {platform} <- clip {clip_id}")
        if post.get("why"):
            print(f"    rationale: {post['why']}")
        if brief:
            print(f"    brief    : {brief}")

        if not dry_run and brief:
            _rewrite(cfg, copywriter, clip_id, brief)

        ok = publish.publish_one(
            cfg, clip_id, platform,
            hashtags=hashtags or None,
            dry_run=dry_run,
            catch_up=bool(post.get("catch_up")),
        )
        if ok:
            posted += 1

    return posted


def _rewrite(cfg: Config, copywriter, clip_id: int, brief: str) -> None:
    """Regenerates one clip's copy under the planner's direction."""
    with db.connect(cfg.paths.database) as conn:
        clip = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if clip is None:
            return

        try:
            context = json.loads(clip["context"] or "{}")
        except json.JSONDecodeError:
            context = {}

        copywriter.add_timing(context, clip, cfg)

        # The on-screen text is already burned into the rendered video and
        # cannot change here. Without telling the writer what it says, a brief
        # that points at a different mechanic produces a post whose caption
        # contradicts the words visible in its own thumbnail.
        try:
            onscreen = [b.get("text", "") for b in json.loads(clip["beats"] or "[]")]
        except (json.JSONDecodeError, AttributeError):
            onscreen = []
        if onscreen:
            brief = (
                f"{brief}\n\nText already burned into this video, which you "
                "cannot change: " + " / ".join(onscreen) + "\nYour post text "
                "must sit alongside it without contradicting it."
            )

        copy = copywriter.write_copy(cfg, context, brief=brief)

        # Post text only. `beats` is left alone: those are burned into the
        # video, which is already rendered, and re-rendering to change them
        # would cost minutes per post.
        conn.execute(
            "UPDATE clips SET hook = ?, caption = ?, hashtags = ? WHERE id = ?",
            (
                copy.hook,
                json.dumps(copy.captions),
                json.dumps({
                    "hashtags": copy.hashtags,
                    "youtube_title": copy.youtube_title,
                }),
                clip_id,
            ),
        )


def apply_weights(cfg: Config, changes: dict[str, float], *, verbose: bool = True) -> int:
    """
    Applies proposed weight changes, clamped.

    Same 4x band as `learn --apply`. An unattended planner nudging a weight
    every run could otherwise walk it to an extreme over a few months and
    collapse the feed onto one kind of clip.
    """
    if not (cfg.orchestrator.allow_weight_changes and changes):
        return 0

    applied = 0
    for name, value in changes.items():
        if name not in cfg.selection.weights:
            continue
        old = cfg.selection.weights[name]
        new = max(old / 4, min(old * 4, float(value)))
        if abs(new - old) >= 0.05:
            cfg.selection.weights[name] = round(new, 2)
            applied += 1
            if verbose:
                print(f"  weight {name}: {old:.2f} -> {new:.2f}")

    if applied:
        cfg.write_default()
    return applied
