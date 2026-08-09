"""
Loads the copywriter's prompt text from an editable file.

The prompts are the part of this system you actually iterate on, so they live in
prompts.md at the repo root rather than buried in Python. The defaults below are
the fallback when that file is missing or a section has been deleted, so a typo
degrades to the shipped wording instead of producing an empty prompt.

Sections are markdown h2 headings. Placeholders are {name} and are substituted
from the values passed to render(); an unknown placeholder is left alone rather
than raising, so stray braces in prose cannot break a run.
"""

from __future__ import annotations

import re
from pathlib import Path

SECTION_RE = re.compile(r"^##[ \t]+([a-z0-9_]+)[ \t]*$", re.MULTILINE)
PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}")

DEFAULTS: dict[str, str] = {
    "system": """
You write the on-screen text for short gameplay clips of {brand}, a 2D momentum
platformer made by {team}. The player swings on a grappling hook, runs along
walls, bounces off jellyfish and dashes through collapsing caves, sometimes
while a giant worm chases them.

What follows is what the game's events mean, the studio's own history, and the
display limits the text has to physically fit inside. There is no house style
and no list of things to avoid — what to say, and how, is yours to decide.

{glossary}

{studio}

{beats}
""".strip(),

    "glossary": """
What the event names mean, in terms a viewer would recognise:

- jelly_combo (count N): the player bounced off N floating glowing jellyfish in
  a row without ever touching the ground. It's not a huge achievement and some levels require it, 
  don't mention it unless the count is very high, but never mention the count because players do not know
  it boosts their speed
- wallrun (duration D): sprinting along a sheer cave wall for D
  seconds without falling. It's pretty standard and it's a way for players to be safe.
- grapple_attach: firing a grappling hook at a point and swinging from it.
- worm_near (distance): an enormous worm is chasing the player through the cave
  and just came within that distance of eating them. Smaller is closer.
- projectile_near_miss (kind, distance, speed): something the worm threw went
  past the player without hitting them. `kind` is what it was — a fireball, a
  missile, the laser, a falling rock, falling debris. `distance` is how close in
  world units, detected within 3, so under 1 is genuinely a hair's breadth. The
  player survived: this is a dodge, not a death.
- wall_near_miss (distance, speed): the player threaded past solid rock at speed
  without touching it. Only reported under 0.7 units and above speed 18, so
  every one of these is already a real squeeze — a run of several minutes
  usually produces one or two. Note this is NOT wall-running — that is
  deliberate contact with the wall, whereas this is missing one.
- death (cause): the player died. The cause is the thing that killed them --
  "the worm", "spikes", "a fireball", "the cave walls" and so on.
- shield_hit: took a hit that a shield absorbed, so they survived it.
- victory: finished the level, except on endless where the player died.
- powerup_pickup: picked up a new ability.
- checkpoint: passed a checkpoint. Boring on its own; never the subject.

- bonus_pickup (name, pending, validated): grabbed one of the three diamonds
  hidden in the level. They are optional, well off the safe route, and taking
  one is always a deliberate risk.
- bonus_lost (count, held_seconds, kept): had a diamond in hand and died before
  reaching the checkpoint that banks it, so it went back where it was. This is
  the game's cruellest rule and the best story in the log. `held_seconds` is how
  long they held it — a few seconds means the detour is what killed them.
- bonus_secured (count, total): reached a checkpoint with diamonds in hand, so
  they finally count. The payoff after the risk.
- speed_peak (speed): top speed of a fast stretch. Median play is around 15;
  these are only reported above 34 and can pass 55.
- input_flurry (presses, seconds): that many separate button presses in that
  many seconds. Panic, or a very technical stretch. Says nothing about the
  outcome — check what else is in the clip.
- ghost_lead_change (leader, margin): the translucent figure alongside is the
  player's own previous best run on this level, replayed against the clock.
  `leader` says who is ahead now.

- menu_open / menu_close (duration): the power-up choice screen. Collecting an
  ability opens it and the game pauses ITSELF — the player did not pause and is
  not taking a break. They are picking between two upgrades, and the clip zooms
  out here so the cards are readable. Rarely worth a caption of its own.
""".strip(),

    "studio": """
The developer's own notes are below. If any relates to what is happening in this clip, use it — a true story always beats an invented one, and only these are known to be true:

## How the project was built

### 1. Making quick bucks

After the huge commercial success of *Kraken Smash: Volleyball* (joking — it only made $5000), the studio decided that if it wanted to survive, it needed to make money quickly.

Both founders were fans of *Spider-Man 2* on GameCube, so the plan was a small **mobile** game built around a grappling hook and a little guy running away from a monster. A prototype existed **4 months later**. Everything was according to plan. But then…

### 2. Being in an incubator

Three realizations killed the mobile plan:

- Players tend to hide their phone screen with their fingers while playing — which makes releasing a *precision* platformer on mobile very hard.
- The mobile market is saturated with big corporations; indie developers have close to zero chance of being noticed.
- The team was pretty good at *making* games, and very bad at *selling* them.

Lost "as a chicken who just lost its head", Léonce showed the project around and the studio was accepted into an incubator — **La Plaine Image, by Game In**, a northern French hub for creative startups — with the hope of finding a publisher and making a proper PC game. The "quick bucks" plan was dropped there.

### 3. Looking for a publisher

Incubation surfaced the cold hard truth of indie dev: roughly **one publisher for 1000 indie projects**, and this project was not the 1-in-1000.

Over **2 years and 5 conventions** the team pitched the game to publishers and met other indie devs and market actors ("small goldfishes released for the first time in the ocean"). The conclusion: a platformer was a terrible commercial idea, pixel art made it worse, and publishers didn't even need to look at the game to know it would fail.

### 4. Releasing the game anyway

With no funding coming, the team still had a solid demo and fans waiting for a release, and did not want to let them down.

The decision: **ship one third of the game at one third of the price**, and see whether it was profitable enough to fund the rest.

- Léonce built levels.
- Dimitri handled marketing and improved the infinite level in his spare time.

The game was released **October 17th** and was quite successful for an indie game. It is still too early to tell whether that will be enough to fund Part 2 — the team is currently improving Part 1 and fixing bugs.

## Where it fits in the studio timeline

- Léonce and Dimitri met in primary school, saw a game studio's creations through a classmate's parent, and vowed to open a studio together as grown-ups.
- ~20 years later (Dimitri a software engineer at an astrophysics institute, Léonce a comic book artist), Covid happened, they remembered the vow, and started small with **Kraken Smash: Volleyball** — released on Steam in 2021, ported to Nintendo Switch in 2022. That project was the "extended tutorial": learning Unity, a build pipeline, shipping, localization, lotcheck, and reacting to player feedback.
- **This Is No Cave** was the next step: the incubated project, the publisher hunt, and the studio's first self-funded commercial release.

## Studio values applied to the project

Trial & error experimentation · Sense of humor · Cooperation first · What you see is what you get — plus having fun while working, which the team considers non-negotiable for making video games.
""".strip(),

    "beats": """
Write the on-screen beats for this clip, in order, each timed in seconds from
the start of the clip.

These are display limits, not editorial direction. They exist because the text
is burned into a vertical video and has to be readable at a glance:

- Every line stays on screen for a full five seconds, so the viewer can read it
  while also watching the gameplay. Beats must therefore be at least five
  seconds apart; anything closer is pushed apart or dropped.
- Stay within the number of beats the clip data says will fit. Beyond that they
  are dropped before anyone sees them.
- At most 70 characters per line, or it overflows the frame.
- The first beat should land within the first second.
- `hook` must repeat beat 1's text exactly; it is used where only one line fits.
""".strip(),
}


FILE_HEADER = """<!--
Prompt text for the shorts copywriter. Edit freely — it is read at run time, so
there is nothing to rebuild. Run `shorts write` again to see the effect.

Rules:
  * Sections are the `## name` headings below. Renaming or deleting one makes
    the pipeline fall back to its built-in default for that section.
  * {placeholders} are filled in from shorts.yaml and the clip's data. Leave
    them in place; an unrecognised one is passed through untouched.
  * `shorts prompts --reset` restores this file to the shipped defaults.
-->
"""


def parse(text: str) -> dict[str, str]:
    """Splits a prompts file into its named sections."""
    sections: dict[str, str] = {}
    matches = list(SECTION_RE.finditer(text))

    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        if body:
            sections[match.group(1)] = body

    return sections


def load(path: Path | None) -> dict[str, str]:
    """Bundled defaults, overlaid with whatever the file defines."""
    prompts = dict(DEFAULTS)

    if path and Path(path).exists():
        try:
            prompts.update(parse(Path(path).read_text(encoding="utf-8")))
        except OSError as exc:
            print(f"  !! could not read {path} ({exc}); using built-in prompts")

    return prompts


def fill(template: str, **values: str) -> str:
    """
    Substitutes {name} placeholders, leaving unknown ones untouched.

    str.format would raise on any stray brace in the prose, which would turn a
    typo in an edited prompt into a crashed run.
    """
    return PLACEHOLDER_RE.sub(
        lambda m: str(values.get(m.group(1), m.group(0))), template
    )


def write_default(path: Path) -> Path:
    """Writes the shipped prompts out as an editable file."""
    order = ["system", "glossary", "studio", "beats"]

    parts = [FILE_HEADER]
    for name in order:
        parts.append(f"## {name}\n\n{DEFAULTS[name]}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
