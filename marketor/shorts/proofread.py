"""
The editor pass.

The writer is a cheap model producing a lot of text, and cheap models make the
same few kinds of mistake: a dropped article, a doubled word, a plural that does
not agree, a sentence that parses but no native speaker would say. None of those
are visible in the JSON; all of them are visible burned into a video.

So a second, stronger model reads the finished copy and fixes mechanics only.
It is explicitly not allowed to have an opinion about the content — not the
angle, not the joke, not the claim. The writer chose those, under a brief from
the planner, and an editor that quietly rewrites them turns two deliberate
decisions into one model's preferences.

The hard part is the house style, which is full of things that look like errors
and are not: sentence fragments, missing final periods, lowercase openings.
Left to its own instincts an editor sands all of that into clean, dead prose,
so most of the prompt is about what to leave alone.
"""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING, Any

from . import env
from .config import Config

if TYPE_CHECKING:
    from .copywriter import Copy

#: Beats are timed against the video by character count, so a beat that grows
#: materially no longer fits its slot. Corrections stay close to the original
#: length or they are dropped.
BEAT_LENGTH_TOLERANCE = 0.25

_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "description": (
                "Every field you were given, in the same order, corrected where "
                "needed and byte-identical where not."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "fixed": {
                        "type": "string",
                        "description": (
                            "What was wrong, in a few words. Empty string if "
                            "you changed nothing."
                        ),
                    },
                },
                "required": ["id", "text", "fixed"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fields"],
    "additionalProperties": False,
}

_SYSTEM = """\
You are copy-editing short marketing text for a video game. Another writer wrote
it to a brief. Your job is mechanics only.

FIX:
- Spelling, including the game's own terms as spelled in the surrounding text.
- Grammar: agreement, tense, articles, prepositions, pronoun case.
- Doubled words, missing words, words in the wrong order.
- Punctuation that is actually wrong, and mismatched quotes or brackets. Wrong
  means it changes or obscures the meaning, not that you would have chosen
  differently.
- Phrasing no fluent speaker would produce — a sentence that parses but reads
  as translated.

DO NOT:
- Change the meaning, the angle, the subject, or any claim. If a line says
  something you think is wrong or unwise, leave it exactly as it is. That is
  not your call.
- Improve, sharpen or punch up anything. A weak joke stays a weak joke.
- Change the register. This is deliberately casual.
- Reorder or merge lines, or move an idea from one field to another.
- Add or remove hashtags, emoji, or trailing punctuation.
- Change the length noticeably. Some of these lines are timed against video by
  character count, so a longer line no longer fits.

NOT ERRORS — leave every one of these alone:
- Sentence fragments. "Six jellyfish. No ground." is the house voice.
  A fragment being deliberate does not make it exempt: "not accident" is still
  missing an article and "Is a lie, but a nice one" is still missing a subject.
  Fragments are fine, ungrammatical ones are not — judge the words that are
  there, not the ones a full sentence would have needed.
- A missing period at the end of a line.
- A lowercase first word, if that is how the line was written.
- Starting a sentence with And, But or So.
- Slang, gaming jargon, and deliberate understatement.
- Second person, and addressing the viewer directly.
- Numerals instead of spelled-out numbers.
- Colloquial pronoun case in a compound subject: "me and the walls have
  history", "the walls and us go way back". These are how people speak, and
  "correcting" them to "the walls and we" produces something no one has ever
  said. Only fix pronoun case where the result sounds more natural, not less.
- Typography. A hyphen where you would use an em dash, straight quotes,
  spacing around a dash, three dots instead of an ellipsis: all fine, leave
  them. Never substitute one dash or quote character for another.
- Game and genre compounds, however they are spaced or hyphenated:
  wallrunning, wall running, wall-run, hitbox, speedrun, grapple. These are the
  project's own vocabulary. Do not respace, hyphenate or "normalise" them, and
  do not make them consistent with each other — only fix one if it is genuinely
  misspelled.

Return every field you were given, in the same order and with the same id. If a
field is already correct, return its text unchanged and leave `fixed` empty.
Most fields should come back unchanged; correcting things that are not broken
is the failure mode here.\
"""

#: Appended per run. The game's name is styled the way its own store page
#: styles it, so "correcting" the capitalisation is a change to a brand, not a
#: fix — and it is the mistake an editor reaches for first.
_BRAND_RULE = (
    "\n- The game is called {brand!r}, styled exactly like that. Never change "
    "its capitalisation, spacing or punctuation, wherever it appears."
)


def _collect(copy: Copy) -> list[tuple[str, str]]:
    """Flattens the copy into (id, text) pairs, skipping empties."""
    fields: list[tuple[str, str]] = [
        ("hook", copy.hook),
        ("youtube_title", copy.youtube_title),
    ]
    for platform in sorted(copy.captions):
        fields.append((f"caption.{platform}", copy.captions[platform]))
    for i, beat in enumerate(copy.beats):
        fields.append((f"beat.{i}", str(beat.get("text", ""))))

    return [(name, text) for name, text in fields if text and text.strip()]


def _apply(copy: Copy, name: str, text: str) -> bool:
    """Writes one corrected field back. Returns whether it landed."""
    if name == "hook":
        copy.hook = text
    elif name == "youtube_title":
        copy.youtube_title = text
    elif name.startswith("caption."):
        platform = name.split(".", 1)[1]
        if platform not in copy.captions:
            return False
        copy.captions[platform] = text
    elif name.startswith("beat."):
        try:
            index = int(name.split(".", 1)[1])
        except ValueError:
            return False
        if not 0 <= index < len(copy.beats):
            return False
        copy.beats[index]["text"] = text
    else:
        return False
    return True


def check(cfg: Config, copy: Copy, *, verbose: bool = True) -> list[str]:
    """
    Corrects the copy in place. Returns a note per change made.

    Never raises and never blocks: the copy is already usable, and an editor
    that can fail the pipeline is worse than a typo.
    """
    from .copywriter import PROVIDERS

    if not cfg.orchestrator.proofread:
        return []

    env.ensure_loaded()
    preset = PROVIDERS.get(cfg.orchestrator.provider, PROVIDERS["openai"])
    api_key = os.environ.get(preset["api_key_env"])
    if not api_key:
        return []

    fields = _collect(copy)
    if not fields:
        return []

    try:
        from openai import OpenAI
    except ImportError:
        return []

    payload = [{"id": name, "text": text} for name, text in fields]
    kwargs: dict[str, Any] = {
        "model": cfg.orchestrator.model,
        "max_completion_tokens": 4000,
        "messages": [
            {"role": "system",
             "content": _SYSTEM + _BRAND_RULE.format(brand=cfg.copy.brand)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
    }
    if preset["schema_mode"] == "json_schema":
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "proofread", "schema": _SCHEMA, "strict": True},
        }
    else:
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"][0]["content"] += (
            "\n\nReply with a single json object matching this schema:\n"
            + json.dumps(_SCHEMA, indent=2)
        )

    try:
        client = OpenAI(api_key=api_key, base_url=preset["base_url"])
        response = client.chat.completions.create(**kwargs)
        returned = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        if verbose:
            print(f"       !! proofread skipped: {exc}")
        return []

    original = dict(fields)
    changes: list[str] = []

    for item in returned.get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("id", ""))
        text = str(item.get("text", ""))
        was = original.get(name)

        if was is None or not text or text == was:
            continue

        # An editor that returns an empty or wildly different beat has stopped
        # editing and started writing. Length is the cheapest signal for that,
        # and beats are the field where it actually breaks something.
        if name.startswith("beat.") and was:
            ratio = abs(len(text) - len(was)) / len(was)
            if ratio > BEAT_LENGTH_TOLERANCE:
                if verbose:
                    print(f"       -- rejected {name}: length changed {ratio:.0%}")
                continue

        if _apply(copy, name, text):
            note = str(item.get("fixed") or "").strip()
            changes.append(f"{name}: {was!r} -> {text!r}" + (f"  ({note})" if note else ""))

    if verbose and changes:
        # Belt and braces on top of the CLI's stdout reconfiguration: this
        # prints arbitrary model text, and a correction that cannot be encoded
        # must never be the thing that ends the run. The correction itself has
        # already been applied by this point.
        print(f"       proofread fixed {len(changes)}:")
        for line in changes:
            try:
                print(f"         {line}")
            except UnicodeEncodeError:
                encoding = getattr(sys.stdout, "encoding", "ascii") or "ascii"
                safe = line.encode(encoding, "replace").decode(encoding, "replace")
                print(f"         {safe}")

    return changes
