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

from . import env, prompts
from .config import Config

if TYPE_CHECKING:
    from .copywriter import Copy

#: Beats are timed against the video by character count, so a beat that grows
#: materially no longer fits its slot. Corrections stay close to the original
#: length or they are dropped.
BEAT_LENGTH_TOLERANCE = 0.25

#: A repair that adds context is allowed to grow past that tolerance, because
#: the missing words are the entire fix. It is not allowed past what the caption
#: renderer can fit: two wrapped lines at the caption width, which is the point
#: where a line starts eating the gameplay it is describing.
ONSCREEN_MAX_CHARS = 68

#: YouTube truncates past this, and a title cut mid-word looks broken.
TITLE_MAX_CHARS = 100

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
                    "kind": {
                        "type": "string",
                        "enum": ["none", "mechanics", "context"],
                        "description": (
                            "'context' for any rewrite beyond mechanics: adding "
                            "the words an incoherent line was missing, cutting a "
                            "contradiction, removing game jargon, or correcting "
                            "the studio's voice. 'none' when unchanged."
                        ),
                    },
                },
                "required": ["id", "text", "fixed", "kind"],
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
- The game's internal vocabulary, wherever it appears: "burst", "wallrun",
  "wall-run", "wall running", "waveburst". Nobody outside the project knows what
  these are, and a viewer three seconds into a short will not learn. Rewrite the
  line to say what it looks like — "riding the wall", "off the wall at speed",
  "never touching the ground" — or drop the clause if the sentence survives
  without it. Never leave the term in. "Grappling hook", "cave", "worm",
  "missile", "shield" and "diamond" are things a viewer can see and are fine.
- "you" or "your" used for the person playing. The studio recorded its own run,
  so the person at the controls is one of the people writing: "I" and "me".
  Addressing the viewer hands them a run they did not play. Worked examples:

      "That missile missed you by 9cm."      -> "That missile missed me by 9cm."
      "You just hit 80 km/h."                -> "I just hit 80 km/h."
      "You're the first to escape."          -> "I'm the first to escape."

  This is not about every "you" in the copy. "You" aimed at the reader about
  something other than the run is fine: "you're welcome", "wait until you see
  this", "the cave hates you for it" said of the game rather than the run. The
  test is whether the sentence credits the viewer with something the player did.
- "he", "him", "she", "her" or "they" used for the person playing. You are given
  every line of one clip at once, so read them as one script. There is no
  narrator here and no character has been introduced: the studio says "we", and
  the person at the controls is "you". A third-person pronoun for the player
  therefore refers to nobody, however natural the sentence sounds. Worked
  example, all four lines of one clip:

      "We wanted a quick mobile game with a grappling hook."
      "He just hit 56 km/h. In a cave. On purpose."
      "That missile missed by 0.018 meters. We counted."
      "The cave wants him dead. We just animate it."

  "He" and "him" have no antecedent anywhere, and the viewer spends the line
  wondering who that is. Both become "me" or "I": the studio recorded its own
  run, so the person playing is one of the people writing. "You" is wrong for
  the same reason - it hands the run to the viewer, who did not play it.

  The exception is a line that names who it means first ("our tester", "the
  player"): then the pronoun has something to point at and can stay.
- A caption that never names the game. This applies to the `caption.*` fields
  only, never to a beat or a title. The caption is the one place a viewer who
  liked the clip can find out what they were watching, and "our own game" sends
  them to a search box with nothing to type. Work the name in where it reads
  naturally, spelled exactly as the brand rule below gives it, and only once.
  If the caption already names it, leave it alone. A hashtag does not count:
  "#thisisnocave" is not the game being named, it is a tag, and a reader
  skimming the sentence never sees it.
- The studio written about in the third person. The copy is the studio talking,
  so "they wanted a quick mobile game" is wrong and "we wanted a quick mobile
  game" is right. Fix the pronoun and any verb that has to agree with it; change
  nothing else about the sentence.
- Hashtags inside a caption, on two counts only. Remove one that repeats
  another in the same caption, keeping the first. Remove one the facts
  contradict — a speed tag on a clip with no fast stretch, a no-death tag on a
  clip that ends in a death. Never add a hashtag, never reorder them, and never
  restyle one: which tags to use is decided elsewhere, and you are only taking
  out the ones that are wrong.
- A line that does not make sense as written: a pronoun with nothing to refer
  back to, a reference to something never introduced, a comparison with only
  one side, a clause that contradicts the one before it. A writer working at
  speed produces these, and unlike a typo they cannot be fixed by moving a
  letter.
- A comparison whose two halves are not the same kind of thing. These are the
  hardest to catch because they are grammatical, and they are the most common
  failure in a compressed punchline. Worked example:

      "the fireball got closer than the publishers ever did"

  The first half is a fireball approaching the player. The second is the studio
  approaching a publisher. The sentence swaps who is approaching whom halfway
  through and ends up asserting nothing. Rebuild the shorter half until both
  sides measure the same thing:

      "the fireball got closer than we ever did to a publisher"

  Read every comparison this way: name what each side is measuring, and if the
  two are not the same quantity, the line is broken however well it scans.

ADDING CONTEXT
When a line is incoherent because something is missing rather than wrong, you
may add the smallest number of words that make it land. Two conditions, both
absolute:

- Every added word must come from the FACTS block or the BACKGROUND block that
  follow the copy. FACTS is the event log of the clip these lines describe: it
  is the only thing you know about the video, and the reader is watching the
  video. BACKGROUND is what the writer was told about the game and the studio,
  and it is where a line that turns out to be a joke about the studio rather
  than about the clip gets repaired — the compressed half of a punchline is
  the most common thing you will have to rebuild.
- If the facts do not contain what the line needs, leave the line exactly as it
  is and say why in `fixed`. A line that reads oddly is survivable. A line that
  states something the footage does not show is not, and you cannot see the
  footage.

Some incoherent lines are repaired by removing words rather than adding them.
A clause that contradicts the one before it, a qualifier that cancels the
sentence it is attached to, half a thought left over from a rewrite: cut it.
That adds no claim and needs no facts, so it is always available to you — and
where a line can be fixed either way, cutting is the safer of the two.

Use `kind` "context" for these two as well — removing jargon and correcting the
voice both change wording rather than mechanics, and they are held to the same
length limit as any other rewrite.

Set `kind` to "context" for these and keep them short — the result is burned
into the video and timed by its length. Everything else is "mechanics", and an
unchanged field is "none".

DO NOT:
- Change the meaning, the angle, the subject, or any claim. If a line says
  something you think is wrong or unwise, leave it exactly as it is. That is
  not your call. Adding context is the one exception, and it clarifies a claim
  rather than replacing it: if your fix changes what the line asserts, it is
  not a fix.
- Use the facts to make a line better. They are there to repair lines that do
  not parse, not to enrich lines that read fine. A correct line stays as it is
  even when the facts would let you sharpen it.
- Improve, sharpen or punch up anything. A weak joke stays a weak joke. A joke
  whose punchline does not parse is a different matter: that is not a weak
  joke, it is a broken sentence, and it is yours to rebuild — keeping the joke
  the writer was making, not substituting one of your own.
- Change the register. This is deliberately casual.
- Reorder or merge lines, or move an idea from one field to another.
- Add a hashtag, or add or remove emoji or trailing punctuation. Removing a
  hashtag is allowed in the two cases listed above and nowhere else.
- Change the length noticeably. Some of these lines are timed against video by
  character count, so a longer line no longer fits.

NOT ERRORS — leave every one of these alone:
- Sentence fragments. "Six jellyfish. No ground." is the house voice.
  A fragment being deliberate does not make it exempt: "not accident" is still
  missing an article and "Is a lie, but a nice one" is still missing a subject.
  Fragments are fine, ungrammatical ones are not — judge the words that are
  there, not the ones a full sentence would have needed.
- A comparison whose second half is obvious and left unsaid. "We flinched
  harder", "it went further", "the cave won again" — English does this
  constantly and the reader supplies the missing term without noticing. This is
  not the broken-comparison case above: that one names both halves and they
  measure different things. If a term is simply absent and the sentence still
  lands, leave it.
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
- Hashtag capitalisation, the game's included. #ThisIsNoCave is a hashtag with
  its word boundaries made visible, not the name miscapitalised in a sentence.
  Every platform here treats it as identical to #thisisnocave, and the camel
  case is what makes it readable. Leave every hashtag exactly as written.
- Em dashes and en dashes, everywhere they appear. Replace with a plain
  hyphen, a comma, a colon or a full stop, whichever the sentence wants. This is
  the one typographic substitution you make, and it is not about taste: a dash
  in short social copy now reads as a tell that the text was machine-written,
  which is the single thing this account cannot afford to look like.
- Typography otherwise. A hyphen where you would use an em dash, straight quotes,
  spacing around a dash, three dots instead of an ellipsis: all fine, leave
  them. Never substitute one quote character for another.
- Game and genre compounds, however they are spaced or hyphenated:
  wallrunning, wall running, wall-run, hitbox, speedrun, grapple. These are the
  project's own vocabulary. Do not respace, hyphenate or "normalise" them, and
  do not make them consistent with each other — only fix one if it is genuinely
  misspelled.

Return every field you were given, in the same order and with the same id. If a
field is already correct, return its text unchanged and leave `fixed` empty.
Most fields should come back unchanged.

There are two ways to fail here and they pull in opposite directions. Sanding
down lines that were never broken is one. Waving through a line that cannot be
understood is the other, and that one ends up burned into a video where nobody
can fix it. A fragment, a lowercase opening, a missing period or an odd rhythm
is not broken. A sentence whose subject is missing, that refers to something
never mentioned, or that says a thing and then unsays it, is broken — repair it
or cut the broken half.\
"""

#: Appended per run. The game's name is styled the way its own store page
#: styles it, so "correcting" the capitalisation is a change to a brand, not a
#: fix — and it is the mistake an editor reaches for first.
_BRAND_RULE = (
    "\n- The game is called {brand!r}, styled exactly like that. Never change "
    "its capitalisation, spacing or punctuation, wherever it appears in running "
    "text. This does not extend to hashtags: inside a hashtag the name is being "
    "made readable, not miscapitalised, and it stays exactly as written."
)


def _background(cfg: Config) -> str:
    """
    The studio and glossary notes the writer worked from.

    Some copy is a joke about the studio rather than a description of the clip,
    and those are the lines that break hardest: a punchline compressed until the
    second half no longer parses. Repairing one needs to know what it was
    referring to, and the clip's event log does not contain that. This is the
    same material the writer had, so the editor is reasoning from the writer's
    own sources rather than guessing at the reference.
    """
    try:
        text = prompts.load(cfg.copy.prompts_file)
    except Exception:
        return ""
    parts = [text.get("glossary", ""), text.get("studio", "")]
    return "\n\n".join(part for part in parts if part)


def _facts(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    The subset of the clip's event log the editor is allowed to draw on.

    Trimmed rather than passed whole, for two reasons. The full context carries
    session-wide totals and prompt scaffolding that describe the recording
    rather than this clip, and an editor repairing a line about *this* moment
    should not be able to reach for a number from somewhere else in the run.
    And a shorter block is a block that gets read.
    """
    if not context:
        return None

    facts: dict[str, Any] = {}
    for key in (
        "trigger", "trigger_data", "scene", "peak_speed", "mean_speed",
        "deaths_in_window", "duration",
    ):
        if context.get(key) not in (None, "", {}, []):
            facts[key] = context[key]

    level = context.get("level")
    if isinstance(level, dict) and level:
        facts["level"] = level

    events = context.get("nearby_events") or []
    if events:
        facts["events_in_clip"] = [
            {"at": e.get("at"), "name": e.get("name"), **(e.get("data") or {})}
            for e in events[:14]
            if isinstance(e, dict)
        ]

    return facts or None


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


def check(
    cfg: Config,
    copy: Copy,
    *,
    context: dict[str, Any] | None = None,
    verbose: bool = True,
) -> list[str]:
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

    payload: dict[str, Any] = {
        "copy": [{"id": name, "text": text} for name, text in fields]
    }
    facts = _facts(context)
    if facts:
        payload["facts"] = facts

    background = _background(cfg)
    if background:
        payload["background"] = background
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

        kind = str(item.get("kind") or "mechanics")

        # An editor that returns an empty or wildly different line has stopped
        # editing and started writing. Length is the cheapest signal for that.
        #
        # A context repair is exempt from the ratio, because adding the missing
        # words *is* the repair — but not from the absolute ceiling. Past that
        # the caption stops fitting the frame, and a line that does not fit is
        # a worse failure than the one it was fixing.
        onscreen = name.startswith("beat.") or name == "hook"
        if onscreen and was:
            if kind == "context":
                if len(text) > ONSCREEN_MAX_CHARS:
                    if verbose:
                        print(
                            f"       -- rejected {name}: {len(text)} chars, "
                            f"over the {ONSCREEN_MAX_CHARS} that fit"
                        )
                    continue
            else:
                ratio = abs(len(text) - len(was)) / len(was)
                if ratio > BEAT_LENGTH_TOLERANCE:
                    if verbose:
                        print(f"       -- rejected {name}: length changed {ratio:.0%}")
                    continue

        if name == "youtube_title" and len(text) > TITLE_MAX_CHARS:
            if verbose:
                print(f"       -- rejected {name}: {len(text)} chars, over {TITLE_MAX_CHARS}")
            continue

        if _apply(copy, name, text):
            note = str(item.get("fixed") or "").strip()
            # Context repairs are the ones worth a second look in the log: they
            # are the only changes that can add a claim.
            tag = "+context " if kind == "context" else ""
            changes.append(
                f"{tag}{name}: {was!r} -> {text!r}" + (f"  ({note})" if note else "")
            )

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
