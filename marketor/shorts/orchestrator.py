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


def gather(cfg: Config) -> dict[str, Any]:
    """Everything the planner needs to decide, and nothing it doesn't."""
    now = datetime.now(timezone.utc)
    state: dict[str, Any] = {
        "today": now.strftime("%Y-%m-%d"),
        "weekday": now.strftime("%A"),
    }

    with db.connect(cfg.paths.database) as conn:
        # Only clips that could legally be posted.
        state["available_clips"] = [
            {
                "clip_id": c["id"],
                "trigger": c["trigger_event"],
                "score": round(float(c["score"]), 1),
                "seconds": round(float(c["end_s"]) - float(c["start_s"]), 1),
                "hook": c["hook"],
            }
            for c in db.clips_in_state(conn, "ready")
        ]

        # Which platforms are actually postable right now, and why not.
        # Platforms reserved for manual posting are not mentioned at all — a
        # planner that knows about a platform it may not use will keep
        # proposing it.
        platforms = {}
        for platform in cfg.publish.platforms:
            if platform in cfg.orchestrator.manual_platforms:
                continue
            ready, reason = publish.due(conn, cfg, platform)
            configured, missing = True, ""
            try:
                from .publishers import get_publisher

                configured, missing = get_publisher(platform).available(cfg)
            except Exception as exc:
                configured, missing = False, str(exc)

            platforms[platform] = {
                "due": ready,
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
                   c.trigger_event, c.hook
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
        "- `clip_id` must come from available_clips.\n\n"
        "JUDGEMENT, which is what you are actually for:\n"
        "- Do not fill a slot with weak material. An empty posts array is a "
        "valid, and often correct, answer. Score is a strong signal: anything "
        "much below the others in available_clips is probably not worth a slot.\n"
        "- Vary. Repeating a clip type or an angle across consecutive posts is "
        "the main way an automated account becomes obvious. Check recent_posts "
        "before choosing.\n"
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


def plan(cfg: Config, *, verbose: bool = True) -> dict[str, Any] | None:
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

    client = OpenAI(api_key=api_key, base_url=preset["base_url"])
    state = gather(cfg)
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

        used_platforms.add(platform)
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
