"""
Writes the on-screen hook and the per-platform captions for a clip.

The telemetry context is what makes this worth doing with a model at all: it can
say "died to the same trap 11 times" because the event log actually knows that.
Generic hooks would not need an API call.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from . import db, env, proofread, prompts
from .config import Config

PLATFORMS = ("tiktok", "instagram", "youtube")

# Both providers speak the OpenAI chat-completions protocol, so one client
# library covers both. They differ in how output can be constrained:
#
#   json_schema - the API guarantees the response matches the schema.
#   json_object - the API guarantees valid JSON and nothing more. DeepSeek's
#                 chat endpoint documents only text and json_object, so the
#                 schema has to go in the prompt and be checked here instead.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "base_url": None,
        "api_key_env": "OPENAI_API_KEY",
        "schema_mode": "json_schema",
        "example_model": "gpt-5.6-terra",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "schema_mode": "json_object",
        "example_model": "deepseek-v4-flash",
    },
}

# Strict structured outputs, so a malformed response can't reach the renderer.
# Strict mode requires every property listed in `required` and
# additionalProperties: false on every object.
_CAPTION_PROPERTIES = {
    p: {"type": "string", "description": f"The post text for {p}."}
    for p in PLATFORMS
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {
            "type": "string",
            "description": "Beat 1's text, repeated verbatim.",
        },
        "beats": {
            "type": "array",
            "description": (
                "The on-screen lines, in order, timed to the clip. How many is "
                "your call, within the range the clip data gives you."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "One line, at most 70 characters.",
                    },
                    "at": {
                        "type": "number",
                        "description": (
                            "Seconds from the start of the clip when this line "
                            "appears. The first should be 0.3 or less."
                        ),
                    },
                },
                "required": ["text", "at"],
                "additionalProperties": False,
            },
        },
        "fact_used": {
            "type": "string",
            "description": (
                "Whatever this copy was built on, in a few words. Recorded so "
                "the operator can see where the line came from."
            ),
        },
        "youtube_title": {
            "type": "string",
            "description": "At most 70 characters.",
        },
        "captions": {
            "type": "object",
            "properties": _CAPTION_PROPERTIES,
            "required": list(PLATFORMS),
            "additionalProperties": False,
        },
    },
    "required": ["hook", "beats", "fact_used", "youtube_title", "captions"],
    "additionalProperties": False,
}


@dataclass
class Copy:
    hook: str
    youtube_title: str
    captions: dict[str, str]
    hashtags: dict[str, list[str]]
    #: Timed on-screen lines, [{"text": ..., "at": seconds}].
    beats: list[dict] = field(default_factory=list)
    #: The data point the copy was built around. Surfaced so you can tell at a
    #: glance whether the model grounded itself or invented something.
    fact_used: str = ""

    def caption_for(self, platform: str) -> str:
        tags = " ".join(self.hashtags.get(platform, []))
        body = self.captions.get(platform, "")
        return f"{body}\n\n{tags}".strip()


def _describe(context: dict[str, Any]) -> str:
    """Flattens the clip's event context into something worth prompting on."""
    trigger = context.get("trigger", "unknown")
    data = context.get("trigger_data") or {}
    lines = []

    duration = context.get("duration")
    if duration:
        lines.append(f"Clip length: {float(duration):.1f} seconds")

    limit = context.get("max_beats")
    if limit:
        low = 2 if limit > 2 else 1
        lines.append(
            f"Write between {low} and {limit} beats for this clip. Anything "
            f"more will not fit at a readable pace and gets dropped."
        )

    at = context.get("trigger_at")
    if at is not None:
        lines.append(f"The triggering moment happens at {float(at):.1f}s into the clip")

    lines.append(f"Triggering event: {trigger}")

    if data:
        lines.append(f"Event details: {json.dumps(data)}")
    if context.get("scene"):
        lines.append(f"Level: {context['scene']}")
    if context.get("peak_speed"):
        lines.append(f"Peak player speed: {context['peak_speed']}")

    nearby = context.get("nearby_events") or []
    if nearby:
        summary = ", ".join(
            f"{e['name']}@{e['at']}s" for e in nearby[:12] if e.get("name") != trigger
        )
        if summary:
            lines.append(f"Other events in the clip: {summary}")

    level = context.get("level") or {}
    if level:
        lines.append(f"This level so far: {json.dumps(level)}")

    session = context.get("session") or {}
    if session:
        lines.append(f"Whole session: {json.dumps(session)}")

    return "\n".join(lines)


def _system_prompt(cfg: Config) -> str:
    """
    Assembles the system prompt from the editable prompt file.

    Four sections, and no editorial direction among them: what the events mean,
    who made the game, and the display limits the text has to fit. What to write
    is left to the writer — the previous version carried a house style, a list
    of banned constructions and a per-mechanic angle sheet, and the copy read
    like something working through a checklist.
    """
    text = prompts.load(cfg.copy.prompts_file)

    return prompts.fill(
        text["system"],
        brand=cfg.copy.brand,
        team=cfg.copy.team,
        glossary=text["glossary"],
        studio=text["studio"],
        beats=text["beats"],
    )


def _fallback(context: dict[str, Any], cfg: Config) -> Copy:
    """
    Deterministic copy used when the API is unavailable or declines.

    An unattended pipeline should degrade to a plain clip rather than stall, and
    the QC gate still gives a human the chance to reject it.
    """
    trigger = context.get("trigger", "clip")
    data = context.get("trigger_data") or {}

    hooks = {
        "jelly_combo": f"{data.get('count', 'a')}-chain bounce",
        "death": f"Killed by {data.get('cause', 'the cave')}",
        "worm_near": "That was close",
        "wallrun": "Did not touch the ground",
        "victory": "Finally cleared it",
        "powerup_pickup": "New run, new powers",
    }
    hook = hooks.get(trigger, "This is no cave")
    caption = f"{hook} — {cfg.copy.brand}"

    return Copy(
        hook=hook,
        youtube_title=f"{hook} | {cfg.copy.brand}",
        captions={p: caption for p in PLATFORMS},
        hashtags=cfg.copy.hashtags,
    )


def resolve_provider(cfg: Config) -> dict[str, Any]:
    """
    Settings for the configured provider, with per-field overrides applied.

    An unknown provider name falls back to the OpenAI protocol defaults rather
    than failing, since any OpenAI-compatible endpoint works given a base_url.
    """
    # Credentials live in .env. The CLI loads it at startup, but this module is
    # also used directly, and without this the key silently looks unset —
    # which degrades to template copy with no obvious cause.
    env.ensure_loaded()

    preset = dict(PROVIDERS.get(cfg.copy.provider, PROVIDERS["openai"]))

    if cfg.copy.base_url:
        preset["base_url"] = cfg.copy.base_url
    if cfg.copy.api_key_env:
        preset["api_key_env"] = cfg.copy.api_key_env
    if cfg.copy.structured_outputs != "auto":
        preset["schema_mode"] = cfg.copy.structured_outputs

    preset["name"] = cfg.copy.provider
    return preset


def _schema_instructions() -> str:
    """
    The schema as prose, for providers without strict schema enforcement.

    Generated from the same _SCHEMA the strict path uses, so the two can't drift
    apart. The literal word "json" matters: DeepSeek's JSON mode requires it to
    appear in the prompt.
    """
    return (
        "Reply with a single json object and nothing else. No markdown, no code "
        "fences, no commentary.\n\n"
        "Return the DATA, not the schema. Your top-level keys must be exactly: "
        "hook, beats, fact_used, youtube_title, "
        "captions. Do not wrap them under \"properties\", do not echo "
        "\"type\": \"object\", and do not nest the answer under any other key. "
        "Every one of those keys is required.\n\n"
        "The json schema your object must satisfy:\n\n"
        + json.dumps(_SCHEMA, indent=2)
    )


def _repair(data: Any, cfg: Config) -> list[str]:
    """
    Fills in fields that can be derived from what the model did return.

    Only the json_object path needs this, and it earns its place: `hook` is by
    definition the first beat's text, so a response with good beats and no hook
    is complete information presented incompletely. Rejecting it would throw
    away a usable answer and post a template instead.

    Returns the names of the fields it had to reconstruct.
    """
    if not isinstance(data, dict):
        return []

    fixed: list[str] = []

    # The schema is embedded in the prompt on this path, and the model sometimes
    # answers by mirroring it: {"type": "object", "properties": {...the real
    # answer...}}. Other times it wraps the object in a single named key. The
    # content is fine in both cases, just nested — so unwrap rather than reject.
    if "hook" not in data and "beats" not in data:
        for key in ("properties", "clip_copy", "response", "result", "data", "output"):
            inner = data.get(key)
            if isinstance(inner, dict) and ("hook" in inner or "beats" in inner):
                data.clear()
                data.update(inner)
                fixed.append(f"unwrapped from {key!r}")
                break

    def blank(key: str) -> bool:
        return not isinstance(data.get(key), str) or not data[key].strip()

    beats = data.get("beats")
    first = ""
    if isinstance(beats, list) and beats and isinstance(beats[0], dict):
        first = str(beats[0].get("text") or "").strip()

    if blank("hook") and first:
        data["hook"] = first
        fixed.append("hook")

    hook = data.get("hook") if isinstance(data.get("hook"), str) else ""

    if blank("youtube_title") and hook:
        data["youtube_title"] = f"{hook} | {cfg.copy.brand}"
        fixed.append("youtube_title")



    captions = data.get("captions")
    if not isinstance(captions, dict):
        captions = {}
        data["captions"] = captions
    missing = [p for p in PLATFORMS if not isinstance(captions.get(p), str) or not captions[p].strip()]
    if missing and hook:
        for platform in missing:
            captions[platform] = hook
        fixed.append(f"captions[{','.join(missing)}]")

    return fixed


def _validate(data: Any) -> str | None:
    """
    Checks a parsed response against the schema's shape.

    Only needed on the json_object path, where the API guarantees valid json and
    nothing about its contents. Returns a reason string, or None if usable.
    """
    if not isinstance(data, dict):
        return f"expected an object, got {type(data).__name__}"

    for key in ("hook", "youtube_title"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            return f"missing or empty {key!r}"

    captions = data.get("captions")
    if not isinstance(captions, dict):
        return "missing 'captions' object"
    missing = [p for p in PLATFORMS if not isinstance(captions.get(p), str)]
    if missing:
        return f"captions missing platform(s): {', '.join(missing)}"

    beats = data.get("beats")
    if not isinstance(beats, list) or not beats:
        return "missing or empty 'beats' array"
    if len(beats) > 4:
        return f"{len(beats)} beats; at most 4 fit on a clip"
    for i, beat in enumerate(beats):
        if not isinstance(beat, dict):
            return f"beat {i} is not an object"
        if not isinstance(beat.get("text"), str) or not beat["text"].strip():
            return f"beat {i} has no text"
        if not isinstance(beat.get("at"), (int, float)):
            return f"beat {i} has no numeric 'at'"

    return None


def write_copy(
    cfg: Config,
    context: dict[str, Any],
    *,
    recent_hooks: list[str] | None = None,
    brief: str = "",
) -> Copy:
    provider = resolve_provider(cfg)

    api_key = os.environ.get(provider["api_key_env"])
    if not api_key:
        return _fallback(context, cfg)

    try:
        from openai import OpenAI
    except ImportError:
        return _fallback(context, cfg)

    client = OpenAI(api_key=api_key, base_url=provider["base_url"])

    user_content = _describe(context)

    if brief:
        # Direction from the orchestrator. Placed first and labelled as an
        # instruction so it outranks the generic guidance in the system prompt,
        # which is the point of having a planner at all.
        user_content = (
            "DIRECTION FOR THIS POST — follow it:\n"
            f"{brief}\n\n"
            "Clip data:\n" + user_content
        )

    if recent_hooks:
        # Without this every jelly_combo clip converges on the same phrasing,
        # which is what makes a feed look automated.
        listed = "\n".join(f"- {h}" for h in recent_hooks[-8:])
        user_content += (
            "\n\nHooks already used for other clips from this session. Do not "
            f"reuse their wording or structure:\n{listed}"
        )

    system = _system_prompt(cfg)

    if provider["schema_mode"] == "json_schema":
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {"name": "clip_copy", "schema": _SCHEMA, "strict": True},
        }
    else:
        response_format = {"type": "json_object"}
        system = f"{system}\n\n{_schema_instructions()}"

    # One retry. Measured on DeepSeek's json_object mode, roughly one call in
    # ten comes back unusable while the next one on identical input succeeds —
    # so a single retry converts most of those into real copy instead of a
    # template. Nothing is retried when the API itself refuses or errors.
    data: dict[str, Any] | None = None
    problem = "no response"

    for attempt in (1, 2):
        try:
            response = client.chat.completions.create(
                model=cfg.copy.model,
                max_completion_tokens=4000,
                response_format=response_format,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            )
        except Exception as exc:
            print(f"  !! copy generation failed ({exc}); using template")
            return _fallback(context, cfg)

        choice = response.choices[0]
        message = choice.message

        # Structured outputs add a refusal path: the model returns a refusal
        # string instead of schema-shaped JSON. Not worth retrying.
        if getattr(message, "refusal", None):
            print(f"  !! copy generation refused ({message.refusal}); using template")
            return _fallback(context, cfg)

        if not message.content:
            problem = f"empty response (finish_reason={choice.finish_reason})"
            data = None
        else:
            try:
                data = json.loads(message.content)
            except json.JSONDecodeError:
                problem, data = "response was not valid JSON", None
            else:
                # Only json_schema guarantees the shape; everything else gets
                # reconstructed where possible, then checked.
                if provider["schema_mode"] != "json_schema":
                    repaired = _repair(data, cfg)
                    if repaired:
                        print(f"       (filled in: {', '.join(repaired)})")
                problem = _validate(data)

        if problem is None:
            break

        if attempt == 1:
            print(f"  .. {problem}; retrying once")

    if problem is not None or data is None:
        print(f"  !! response unusable after retry ({problem}); using template")
        return _fallback(context, cfg)

    copy = Copy(
        hook=data["hook"].strip(),
        youtube_title=data["youtube_title"].strip(),
        captions=data["captions"],
        hashtags=cfg.copy.hashtags,
        beats=[b for b in (data.get("beats") or []) if str(b.get("text", "")).strip()],
        fact_used=data.get("fact_used", "").strip(),
    )

    # Mechanics only, on the stronger model. Applied here rather than at the
    # call sites so the rewrite path gets it too, and before the caller stores
    # anything: a typo burned into a video costs a re-render to fix.
    proofread.check(cfg, copy)

    return copy


def beat_budget(duration: float, min_hold: float) -> int:
    """
    How many captions this clip can hold at a readable pace.

    Every line has to stay up long enough to read while the viewer is also
    watching the gameplay, so a short clip simply cannot carry four of them.
    Telling the writer the ceiling up front beats generating four and silently
    dropping two, which is what produced a feed of identical three-beat clips.
    """
    return max(1, min(4, int((duration - 1.0) // max(min_hold, 0.1))))


def add_timing(context: dict, clip, cfg: Config | None = None) -> dict:
    """
    Adds the clip's length, where the payoff sits, and how many beats fit.

    The writer needs all three to time the beats against the action. Kept out of
    the stored context because it is derived, and shared with the orchestrator's
    rewrite path so a re-written clip is described identically to a fresh one.
    """
    start, end = float(clip["start_s"]), float(clip["end_s"])
    context["duration"] = round(end - start, 1)
    context["max_beats"] = beat_budget(
        end - start,
        cfg.render.caption_min_seconds if cfg else 5.0,
    )

    if context.get("trigger_at") is None:
        nearby = context.get("nearby_events") or []
        match = next(
            (e for e in nearby if e.get("name") == context.get("trigger")), None
        )
        context["trigger_at"] = match["at"] if match else round((end - start) * 0.7, 1)

    return context


def write_pending(cfg: Config, *, limit: int | None = None, verbose: bool = True) -> int:
    """Generates copy for every selected clip that doesn't have any yet."""
    written = 0

    provider = resolve_provider(cfg)
    key_var = provider["api_key_env"]

    if os.environ.get(key_var):
        print(
            f"  using {provider['name']} / {cfg.copy.model} "
            f"({provider['schema_mode']})"
        )
    else:
        # This used to fail silently, which made every clip come out with the
        # same template hook and looked like the model was just bad at its job.
        print(
            f"  !! {key_var} is not set — falling back to fixed templates.\n"
            "     Captions will be repetitive and generic until you set it in .env."
        )

    recent_hooks: list[str] = []

    with db.connect(cfg.paths.database) as conn:
        clips = db.clips_in_state(conn, "selected")
        if limit:
            clips = clips[:limit]

        for clip in clips:
            try:
                context = json.loads(clip["context"] or "{}")
            except json.JSONDecodeError:
                context = {}

            add_timing(context, clip, cfg)

            copy = write_copy(cfg, context, recent_hooks=recent_hooks)
            recent_hooks.append(copy.hook)

            conn.execute(
                """
                UPDATE clips
                   SET hook = ?, caption = ?, hashtags = ?, beats = ?,
                       state = 'written'
                 WHERE id = ?
                """,
                (
                    copy.hook,
                    json.dumps(copy.captions),
                    json.dumps(
                        {
                            "hashtags": copy.hashtags,
                            "youtube_title": copy.youtube_title,
                        }
                    ),
                    json.dumps(copy.beats),
                    clip["id"],
                ),
            )
            written += 1
            if verbose:
                print(f"  + clip {clip['id']}:")
                for beat in copy.beats:
                    print(f"       {float(beat.get('at', 0)):>5.1f}s  {beat.get('text')}")

    return written
