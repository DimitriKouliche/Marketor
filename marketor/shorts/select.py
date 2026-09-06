"""
Picks which moments become clips.

This is the stage that decides whether the output is worth posting. A clip is
built around one triggering event, widened to include its run-up, then scored on
how much else was happening around it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import analysis, db, telemetry
from .config import Config


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    trigger: str
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "Candidate", gap: float) -> bool:
        return self.start < other.end + gap and other.start < self.end + gap


def _window(
    cfg: Config,
    event: telemetry.Event,
    offset: float,
    session: telemetry.Session | None = None,
    endless: bool = False,
) -> tuple[float, float]:
    """
    Time range around one event.

    Events that report a duration (a wall-run) are logged when they *finish*, so
    the window has to reach back far enough to contain the whole thing.

    The base window is a fixed lead-in and lead-out, which on its own makes
    every clip exactly the same length — a feed of identical 20-second cuts
    reads as machine output whatever the captions say. So the window then grows
    outward through any run of nearby interesting events, which makes length a
    property of what happened: an isolated victory stays short, a stretch with
    four things going on earns the extra seconds.
    """
    at = event.t + offset
    lead_in = cfg.clip.lead_in
    lead_out = cfg.clip.lead_out

    duration = event.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        lead_in = max(lead_in, float(duration) + 2.0)

    if event.name == "victory":
        # The approach is the story; the celebration screen is not.
        lead_in = max(lead_in, 9.0)
        lead_out = min(lead_out, 2.0)

    if endless:
        # Reach further back, and do it after the victory rule above: in endless
        # a "victory" is the run ending, which is the one moment where the run
        # up to it is the whole clip.
        lead_in = max(lead_in, cfg.clip.endless_lead_in)

    if session is None or not cfg.clip.follow_action:
        return at - lead_in, at + lead_out

    first, last = _action_span(cfg, event, at, offset, session)

    # Nothing led into this one. The lead-in exists to make a payoff readable,
    # and past a few seconds of empty cave it stops adding context and starts
    # adding dead air — so an isolated moment gets a shorter clip, which is
    # most of where the variation in length comes from.
    if first >= at - 0.01:
        lead_in = min(lead_in, cfg.clip.quiet_lead_in)

    # Smaller pads than the base lead-in when extending: a clip that opens on
    # other action already has its context.
    return min(at - lead_in, first - 2.0), max(at + lead_out, last + 3.0)


def _action_span(
    cfg: Config,
    event: telemetry.Event,
    at: float,
    offset: float,
    session: telemetry.Session,
) -> tuple[float, float]:
    """
    How far the run of activity around the trigger reaches, in video seconds.

    Walks outward from the trigger and stops at the first quiet gap, so the clip
    ends where the action does rather than at a fixed count of seconds.
    """
    gap = cfg.clip.action_gap
    reach = cfg.clip.max_duration

    times = sorted(
        e.t + offset
        for e in session.events
        if cfg.selection.weights.get(e.name, 0.0) >= cfg.clip.action_min_weight
        and abs(e.t + offset - at) <= reach
    )

    first = last = at
    for t in (t for t in reversed(times) if t < at):
        if first - t > gap:
            break
        first = t
    for t in (t for t in times if t > at):
        if t - last > gap:
            break
        last = t

    return first, last


def _idle_fraction(
    session: telemetry.Session,
    offset: float,
    start: float,
    end: float,
    menus: list[tuple[float, float]],
    threshold: float,
) -> float:
    """
    How much of a window the player spent motionless.

    Someone who walked away still generates events: the worm keeps throwing
    things, the level eventually kills them, and the log looks like a clip. The
    speed curve does not lie about it — a window of an idle player sits at zero.

    Menu time is excluded because the power-up screen freezes the game, which
    reads as perfect stillness and would condemn any clip containing a choice.
    """
    speeds = [
        float(sample.data.get("speed", 0) or 0)
        for sample in session.samples
        if start <= sample.t + offset <= end
        and not any(a <= sample.t + offset <= b for a, b in menus)
    ]
    if len(speeds) < 5:
        return 0.0
    return sum(1 for v in speeds if v < threshold) / len(speeds)


def is_dodge(cfg: Config, session: telemetry.Session, event: telemetry.Event) -> bool:
    """
    Whether a near miss was actually a dodge.

    With twenty missiles converging, one passes close at the same moment
    another lands. The log records both, and the closest distance in the window
    then describes an escape over footage of the player being hit — which is
    the opposite of what happened.

    Two ways to fail. Something hit the player around it, or the "miss" was at
    zero distance, which is a contact whatever the event is called: the
    proximity tracker reports the closest approach of a pass, and a pass that
    connects reports nothing.
    """
    try:
        distance = float(event.get("distance", 9.0) or 9.0)
    except (TypeError, ValueError):
        distance = 9.0
    if distance < cfg.selection.near_miss_min_distance:
        return False

    guard = cfg.selection.near_miss_damage_guard
    return not any(
        e.name in ("shield_hit", "death") and abs(event.t - e.t) <= guard
        for e in session.events
    )


def _attempt_bounds(
    session: telemetry.Session | None, offset: float, at: float
) -> tuple[float | None, float | None, str | None]:
    """
    The attempt the trigger belongs to, bounded by deaths and victories.

    A death or a victory ends the thing the clip is about. Everything after it
    is a different attempt, and it shows: a clip that runs on past its own death
    is two clips glued together, and one that runs past a victory is twenty-six
    seconds of a level that is already over.

    So the window stops at the first terminal event at or after the trigger, and
    starts after the last one before it. That single rule is what keeps a clip
    to one attempt — which is also why the retry-loop cutting that used to patch
    this up afterwards is gone.
    """
    if session is None:
        return None, None, None

    terminal = sorted(
        (e.t + offset, e.name)
        for e in session.events
        if e.name in ("death", "victory")
    )
    before = [t for t, _ in terminal if t < at - 0.01]
    after = [(t, kind) for t, kind in terminal if t >= at - 0.01]
    lo = max(before, default=None)
    hi, kind = after[0] if after else (None, None)
    return lo, hi, kind


def _level_bounds(
    session: telemetry.Session | None, offset: float, at: float
) -> tuple[float | None, float | None]:
    """
    The level the trigger belongs to, in video seconds.

    A clip has to stay inside one attempt. The lead-in is thirteen seconds, so
    an event early in a level reaches back across the level change and opens on
    the last seconds of the previous attempt and its results screen — which
    reads as a clip that starts in the wrong place, because it does.
    """
    if session is None:
        return None, None

    starts = sorted(
        e.t + offset for e in session.events if e.name == "level_start"
    )
    lo = max((t for t in starts if t <= at + 0.01), default=None)
    hi = min((t for t in starts if t > at + 0.01), default=None)
    return lo, hi


def _clamp(
    cfg: Config,
    start: float,
    end: float,
    duration: float,
    bounds: tuple[float | None, float | None] | None = None,
    attempt: tuple[float | None, float | None, str | None] | None = None,
    at: float | None = None,
    menus: list[tuple[float, float]] | None = None,
    min_duration: float | None = None,
) -> tuple[float, float] | None:
    """Fits a window inside the recording, the level, and the allowed length."""
    floor_length = cfg.clip.min_duration if min_duration is None else min_duration
    # Clear of the transition at both ends, so the clip opens and closes on
    # gameplay rather than on a fade or a results screen.
    lo, hi = bounds or (None, None)
    floor = max(0.0, lo + cfg.clip.level_start_margin) if lo is not None else 0.0
    ceiling = (
        min(duration, hi - cfg.clip.level_end_margin) if hi is not None else duration
    )

    # One attempt. The death or victory itself stays in — it is the ending — but
    # nothing after it does, and the respawn fade before the previous one is cut
    # clear: a clip that opens on a fade-in opens on black.
    alo, ahi, akind = attempt or (None, None, None)
    if alo is not None:
        floor = max(floor, alo + cfg.clip.respawn_margin)
    if ahi is not None:
        # A death and a victory need different amounts of room after them.
        # Measured over 608 deaths, the respawn lands 0.57s later at the median
        # and 0.46s at the tenth percentile, so anything past about 0.4s shows
        # the level starting over — which is the next attempt, not this clip.
        # A victory has nothing following it to avoid.
        tail = cfg.clip.death_tail if akind == "death" else cfg.clip.victory_tail
        ceiling = min(ceiling, ahi + tail)

    start = max(floor, start)
    end = min(ceiling, end)

    # The window has to contain the moment it was built around. The margins
    # above can push past it — a respawn margin can start the clip after its own
    # trigger, and the minimum length can end it before one — which produces a
    # clip about a speed peak that does not show the speed peak.
    if at is not None:
        start = min(start, max(floor, at))
        end = max(end, min(ceiling, at))

    if end <= start:
        return None

    length = end - start
    if length > cfg.clip.max_duration:
        # Trim from the front: the payoff is at the end of the window.
        start = end - cfg.clip.max_duration
    elif length < floor_length:
        needed = floor_length - length
        start = max(floor, start - needed)
        end = min(ceiling, start + floor_length)

    # The endless floor is a preference, not a rule: a run that only had
    # eighteen seconds of level left is still worth cutting at eighteen.
    if end - start < min(floor_length, cfg.clip.min_duration):
        return None

    # Last word: if the length rules moved the window off its trigger after all,
    # there is no window here worth having. Another candidate for the same
    # moment usually survives; a clip missing its own payoff never should.
    if at is not None and not (start - 0.01 <= at <= end + 0.01):
        return None

    return start, end


# Death causes arrive as tags or raw GameObject names ("RocksBase"). Handing
# those to the copywriter produces captions nobody outside the project can
# parse, so they are translated to what a viewer actually saw.
_CAUSE_WORDS = {
    "worm": "the worm",
    "fireball": "a fireball",
    "debris": "falling debris",
    "laser": "the laser",
    "spikes": "spikes",
    "jelly": "a jellyfish",
    "missile": "a missile",
}


def humanise_cause(raw: str | None) -> str:
    """Player-facing description of whatever killed them."""
    if not raw:
        return "the cave"

    lowered = raw.lower()
    for key, phrase in _CAUSE_WORDS.items():
        if key in lowered:
            return phrase

    # The level geometry is named RocksBase / RocksExtra / RocksBaseSecret and
    # friends; from the player's side that is all just hitting the cave.
    if "rock" in lowered or "wall" in lowered or "brittle" in lowered:
        return "the cave walls"

    # Last resort: split the CamelCase so at least it reads as words.
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", raw).lower().strip()
    return spaced or "the cave"


#: PlayerPrefs "tier", as written into every level_start.
_TIERS = {0: "chill", 1: "normal", 2: "hardcore"}


def _tier_at(session: telemetry.Session, t: float) -> str | None:
    """
    Which difficulty the run was on. Worth telling the writer about: hardcore
    takes the shield away and speeds the game up, so the same near miss means
    something different there.
    """
    tier = None
    for event in session.events:
        if event.t > t:
            break
        if event.name == "level_start":
            tier = event.get("tier")
    return _TIERS.get(tier) if tier is not None else None


def _scene_at(session: telemetry.Session, t: float) -> str | None:
    """Which level was loaded at a given telemetry timestamp."""
    scene = None
    for event in session.events:
        if event.t > t:
            break
        if event.name == "level_start":
            scene = event.get("scene")
    return scene


def _is_endless(cfg: Config, scene: str | None) -> bool:
    """
    Whether a scene is the endless mode.

    Endless is a different watch. A level clip is one obstacle solved, and it is
    over when it is over; an endless run is an escalation, and the point is how
    far it went. Cut to the same twenty seconds as everything else it reads as a
    fragment of something rather than a run.
    """
    if not scene:
        return False
    lowered = scene.lower()
    return any(name.lower() in lowered for name in cfg.selection.endless_scenes)


def _is_playable(cfg: Config, scene: str | None) -> bool:
    """
    Whether a scene is gameplay at all.

    The game emits level_start for everything it loads, menus and cinematics
    included, so "which level is this" and "is this a level" are different
    questions. A clip anchored in the main menu gets a menu cropped to the
    gameplay window, which is how a title screen ends up half visible in a
    short.
    """
    if not scene:
        return True

    lowered = scene.lower()
    return not any(bad.lower() in lowered for bad in cfg.selection.ignore_scenes)


def _position_at(
    session: telemetry.Session, event: telemetry.Event
) -> list[float] | None:
    """Where the player was when an event fired, from the event or the samples."""
    own = event.get("pos")
    if isinstance(own, (list, tuple)) and len(own) >= 2:
        try:
            return [float(own[0]), float(own[1])]
        except (TypeError, ValueError):
            pass

    nearest = None
    best = None
    for sample in session.samples:
        gap = abs(sample.t - event.t)
        if best is None or gap < best:
            pos = sample.data.get("pos")
            if isinstance(pos, (list, tuple)) and len(pos) >= 2:
                nearest, best = pos, gap
    if nearest is None or best is None or best > 1.0:
        return None
    try:
        return [float(nearest[0]), float(nearest[1])]
    except (TypeError, ValueError):
        return None


def _powerup_build(
    session: telemetry.Session, offset: float, at: float
) -> dict[str, int]:
    """
    The upgrades in play at a given moment, with how many times each was taken.

    Power-ups stack: choosing fireballs three times puts three times as many
    fireballs on screen. So the interesting quantity is the level, not the name,
    and a clip five minutes into an endless run inherits everything chosen
    before it — counting only the choices made inside the clip's own window
    would report an empty build for the most heavily upgraded footage there is.

    Accumulated from the start of the current run, because a new level starts
    over.
    """
    starts = [e.t for e in session.events if e.name == "level_start" and e.t <= at]
    since = max(starts, default=0.0)

    build: dict[str, int] = {}
    for event in session.events:
        if event.name != "powerup_chosen" or not (since <= event.t <= at):
            continue
        name = str(event.get("name") or "")
        if not name:
            continue
        try:
            build[name] = max(int(event.get("level", 2) or 2), build.get(name, 0))
        except (TypeError, ValueError):
            build[name] = build.get(name, 0) + 1
    return build


def _session_facts(cfg: Config, session: telemetry.Session) -> dict[str, Any]:
    """
    Session-wide numbers, computed once per recording.

    This is what lets the copy say something genuinely interesting — "eleventh
    death on this level tonight" is a fact the clip's own events don't contain.
    """
    ignored = set(cfg.selection.ignore_death_causes)
    deaths = [e for e in session.of_type("death") if e.get("cause") not in ignored]
    victories = session.of_type("victory")

    span = session.events[-1].t - session.events[0].t if session.events else 0.0

    causes: dict[str, int] = {}
    for death in deaths:
        cause = humanise_cause(death.get("cause"))
        causes[cause] = causes.get(cause, 0) + 1

    return {
        "deaths": deaths,
        "victories": victories,
        # Named for what they are. Handed over as bare numbers they read as
        # facts about the clip, and the writer duly claimed eighty-five deaths
        # inside an endless run that ends at the first one — every number true,
        # the sentence impossible.
        "summary": {
            "note": (
                "Totals for the whole recording session, across every level and "
                "every attempt. They are NOT about this clip and must never be "
                "attributed to this run."
            ),
            "session_minutes": round(span / 60.0, 1),
            "deaths_this_whole_session": len(deaths),
            "levels_cleared_this_whole_session": len(victories),
            "deaths_by_cause_this_whole_session": dict(
                sorted(causes.items(), key=lambda kv: -kv[1])[:5]
            ),
        },
    }


def _clip_facts(
    session: telemetry.Session, facts: dict[str, Any], event: telemetry.Event, scene: str | None
) -> dict[str, Any]:
    """Facts scoped to this clip's level and moment."""
    if scene is None:
        return {}

    deaths_here = [
        d for d in facts["deaths"] if d.t <= event.t and _scene_at(session, d.t) == scene
    ]
    attempts = sum(
        1 for e in session.of_type("level_start") if e.t <= event.t and e.get("scene") == scene
    )

    out: dict[str, Any] = {
        "deaths_on_this_level_so_far": len(deaths_here),
        "attempt_number_on_this_level": attempts,
    }

    difficulty = _tier_at(session, event.t)
    if difficulty:
        out["difficulty"] = difficulty
    if deaths_here:
        raw = deaths_here[-1].get("cause")
        cause = humanise_cause(raw)
        repeats = sum(1 for d in deaths_here if humanise_cause(d.get("cause")) == cause)
        out["most_recent_death_cause"] = cause
        out["times_killed_by_that_here"] = repeats
    return out


def _score(
    cfg: Config,
    event: telemetry.Event,
    window: tuple[float, float],
    session: telemetry.Session,
    curve: telemetry.SpeedCurve,
    offset: float,
    facts: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    """
    Scores a candidate and collects the context the copywriter will need.

    Three multipliers on top of the event's base weight: how fast the player was
    moving, how much else happened in the same window, and event-specific
    intensity (a five-chain combo beats a two-chain).
    """
    start, end = window
    base = cfg.selection.weights.get(event.name, 1.0)

    peak = curve.peak_speed(start, end)
    speed_mult = 1.0 + min(peak / cfg.selection.fast_speed, 2.0) * 0.5

    nearby = [
        e for e in session.events
        if e.name not in ("sample", "session_start", "session_end", "level_start")
        and start <= e.t + offset <= end
    ]
    # Deaths and level restarts are deliberately excluded. Density is meant to
    # reward a window where a lot of different things happen; counting retries
    # made it reward the opposite — a player dying to the same rock nine times
    # scores as the busiest footage in the session.
    varied = [e for e in nearby if e.name not in ("death", "level_start", "checkpoint")]

    # Counted per unit of time, not in total. Every count-based bonus here
    # rewards a longer window simply for containing more, and endless clips are
    # deliberately the longest — measured, they took eleven of the top twenty
    # places whatever the bonuses were set to, including zero. Rates put a dense
    # fourteen second clip and a dense thirty-six second one on equal terms,
    # which is the comparison that was wanted all along.
    span = max(1.0, end - start)
    per_window = cfg.selection.rate_window / span

    density_mult = 1.0 + min(len(varied) * per_window, 8) * 0.06

    # One death is a story. Nine deaths and ten restarts is a player grinding a
    # section, which is the single most repetitive thing this game produces and
    # the last thing worth putting in front of a stranger.
    deaths = sum(1 for e in nearby if e.name == "death")
    restarts = sum(1 for e in nearby if e.name == "level_start")
    repeats = max(deaths, restarts) - cfg.selection.retry_tolerance
    retry_mult = (
        1.0 / (1.0 + repeats * cfg.selection.retry_penalty) if repeats > 0 else 1.0
    )

    intensity = 1.0
    if event.name == "jelly_combo":
        intensity = 1.0 + float(event.get("count", 2)) * 0.18
    elif event.name == "worm_near":
        distance = float(event.get("distance", 6.0) or 6.0)
        # Closer is better; 1 unit away is a genuine near miss.
        intensity = 1.0 + max(0.0, (6.0 - distance)) * 0.15
    elif event.name == "projectile_near_miss":
        # Detected within 3 units, so anything under 1 was genuinely close.
        distance = float(event.get("distance", 3.0) or 3.0)
        intensity = 1.0 + max(0.0, (3.0 - distance)) * 0.35
    elif event.name == "wall_near_miss":
        # Detected within 0.7 units and 18 speed, so every one of these is
        # already a real squeeze — the game only reports the closest point of
        # each pass. Intensity separates the close ones from the very close.
        distance = float(event.get("distance", 0.7) or 0.7)
        speed = float(event.get("speed", 0.0) or 0.0)
        intensity = (
            1.0
            + max(0.0, (0.7 - distance)) * 1.0
            + min(speed / cfg.selection.fast_speed, 2.0) * 0.15
        )
    elif event.name == "bonus_lost":
        # Time between taking the diamond and dying is the whole signal. Die
        # three seconds after the grab and the detour is what killed you; die
        # forty seconds later and the diamond is incidental. Losing more than
        # one at once is worse, and worse is better footage.
        held = float(event.get("held_seconds", -1.0) or -1.0)
        count = float(event.get("count", 1) or 1)
        recency = 1.0 if held < 0 else max(0.0, (20.0 - held) / 20.0)
        intensity = 1.0 + recency * 1.2 + (count - 1) * 0.4
    elif event.name == "bonus_secured":
        intensity = 1.0 + (float(event.get("count", 1) or 1) - 1) * 0.4
    elif event.name == "speed_peak":
        # Emitted from the 95th percentile up, so the interesting part is how
        # far past that it went.
        speed = float(event.get("speed", 0.0) or 0.0)
        intensity = 1.0 + max(0.0, speed - 34.0) * 0.06

        # Unless the gas did it. A gas explosion multiplies the burst, so the
        # speed that follows is a property of the level, not of the player —
        # the highest-scoring clip in the catalogue turned out to be someone
        # riding gas clouds to the end of the last level.
        gas = any(
            e.name == "gas_burst"
            and 0.0 <= (event.t - e.t) <= cfg.selection.gas_speed_window
            for e in session.events
        )
        if gas:
            intensity *= cfg.selection.gas_speed_discount
    elif event.name == "input_flurry":
        presses = float(event.get("presses", 0) or 0)
        intensity = 1.0 + max(0.0, presses - 9.0) * 0.08
    elif event.name == "ghost_lead_change":
        # Taking the lead is the moment worth watching; losing it is setup.
        intensity = 1.4 if event.get("leader") == "player" else 1.0
    elif event.name == "wallrun":
        # Duration alone ranked a slow drift along a wall level with a fast one.
        # Wall-run speed runs 6.5 to 17 in practice, and the fast end is where
        # the movement tech is, so it carries as much weight as the length.
        speed = float(event.get("speed", 0.0) or 0.0)
        intensity = (
            1.0
            + min(float(event.get("duration", 1.0) or 1.0), 8.0) * 0.1
            + max(0.0, speed - 9.0) * 0.09
        )
    elif event.name == "waveburst":
        # A chain is the demonstration; one is an input that happened to land.
        chain = float(event.get("chain", 1) or 1)
        intensity = 1.0 + (chain - 1) * 0.45

    # Bursts taken off a wall are the rhythm that reads as skill on screen:
    # touch, burst, touch, burst. Judged by eye, the clips with that rhythm were
    # the best in the batch and scored near the bottom, because until the game
    # started logging bursts there was nothing in the window to score.
    wall_bursts = sum(1 for e in nearby if e.name == "burst" and e.get("wall"))
    tech_mult = 1.0 + min(wall_bursts * per_window, 10) * cfg.selection.wall_burst_bonus

    # Screen pressure. A stretch with twenty missiles converging on the player
    # is the clip, and the event log calls it an ordinary run: near misses only
    # count the ones that actually came close, so a swarm that the player
    # outruns cleanly registers as nothing at all.
    #
    # Two measures, because only one of them exists in older recordings. The
    # hazard count is sampled by the game and is exact; the near-miss count is
    # a weaker proxy that every recording already carries.
    peak_hazards = 0.0
    for sample in session.samples:
        when = sample.t + offset
        if start <= when <= end:
            try:
                peak_hazards = max(peak_hazards, float(sample.data.get("hazards", 0) or 0))
            except (TypeError, ValueError):
                pass

    # A near miss is only a dodge if nothing hit the player around it. With
    # twenty missiles converging, one passes close at the same moment another
    # lands, and the log records both — so the writer, handed the closest
    # distance in the window, described a 1.8cm escape over footage of the
    # player being hit 0.36s earlier. Measured on that clip: 24 of 41 near
    # misses had damage within a second.
    all_near = [e for e in nearby if e.name == "projectile_near_miss"]
    dodges = [e for e in all_near if is_dodge(cfg, session, e)]

    # Pressure counts everything on screen; only the clean ones are dodges.
    near_misses = len(all_near)
    pressure_mult = (
        1.0
        + min(max(0.0, peak_hazards - 3.0), 12.0) * cfg.selection.hazard_bonus
        + min(near_misses * per_window, 8) * cfg.selection.near_miss_bonus
    )

    score = (
        base * speed_mult * density_mult * intensity
        * retry_mult * tech_mult * pressure_mult
    )

    scene = _scene_at(session, event.t)

    trigger_data = dict(event.data)
    if event.name in ("death", "shield_hit") and "cause" in trigger_data:
        trigger_data["cause"] = humanise_cause(trigger_data.get("cause"))

    context = {
        "trigger": event.name,
        # Where the payoff sits inside the clip. Computed here because this is
        # the only place that knows both the event and the window; without it
        # the renderer cannot lean the camera into the moment, and for sixty-two
        # clips it silently did not.
        "trigger_at": round(event.t + offset - start, 2),
        # Where in the level this happened. Some events carry no position of
        # their own — a victory does not — and without one they escaped the
        # duplicate check entirely, which is how the same corner came back
        # three times. The player's own position at that instant always exists.
        "trigger_pos": _position_at(session, event),
        "trigger_data": trigger_data,
        "scene": scene,
        # Spelled out rather than left to be inferred from the scene name: the
        # two modes have different rules for diamonds and for difficulty, and
        # the writer described a level mechanic over endless footage.
        "mode": "endless" if _is_endless(cfg, scene) else "level",
        "deaths_in_window": deaths,
        "peak_speed": round(peak, 1),
        # So the writer knows whether the speed is a feat or the level's doing.
        # Upgrades in play when this happened, with their stacked level.
        "powerup_build": _powerup_build(session, offset, event.t),
        "wall_bursts": wall_bursts,
        "hazards_on_screen": int(peak_hazards),
        "projectiles_dodged": len(dodges),
        "speed_from_gas": any(
            e.name == "gas_burst" and start <= e.t + offset <= end
            for e in session.events
        ),
        "mean_speed": round(curve.mean_speed(start, end), 1),
        # The near misses that were not dodges are removed rather than
        # relabelled: left in, they are the closest distance in the window and
        # the writer reaches for them every time.
        "nearby_events": [
            {"name": e.name, "at": round(e.t + offset - start, 2), "data": e.data}
            for e in nearby
            if e.name != "projectile_near_miss" or e in dodges
        ],
        "session": facts["summary"],
        "level": _clip_facts(session, facts, event, scene),
    }
    return score, context


def _spot(cand: Candidate) -> tuple[str, float, float] | None:
    """Where in the level the trigger happened, for duplicate detection."""
    pos = cand.context.get("trigger_pos") or (
        cand.context.get("trigger_data") or {}
    ).get("pos")
    scene = cand.context.get("scene")
    if not scene or not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return None
    try:
        return str(scene), float(pos[0]), float(pos[1])
    except (TypeError, ValueError):
        return None


def _same_spot(a: Candidate, b: Candidate, radius: float) -> bool:
    """
    Whether two candidates happened at the same place in the same level.

    Spacing them in time is not enough. A hard section gets attempted over and
    over, and two runs at the same corner ten minutes apart are the same clip to
    anyone watching the feed — same rocks, same jump, same outcome. Three of the
    five weakest clips in one batch were one corner of one level.
    """
    first, second = _spot(a), _spot(b)
    if first is None or second is None:
        return False
    if first[0] != second[0]:
        return False
    return math.dist(first[1:], second[1:]) <= radius


def _dedupe(
    cands: list[Candidate], min_gap: float, limit: int, spot_radius: float = 0.0
) -> list[Candidate]:
    """
    Greedy highest-score-first selection with a spacing rule.

    Two clips cut from the same ten seconds of footage read as duplicates on a
    feed even when the triggering events differ, so spacing is enforced against
    everything already accepted rather than only the previous pick — and, when a
    radius is given, against where in the level they happened as well as when.
    """
    chosen: list[Candidate] = []
    for cand in sorted(cands, key=lambda c: c.score, reverse=True):
        if any(cand.overlaps(kept, min_gap) for kept in chosen):
            continue
        if spot_radius > 0 and any(_same_spot(cand, kept, spot_radius) for kept in chosen):
            continue
        chosen.append(cand)
        if len(chosen) >= limit:
            break
    return sorted(chosen, key=lambda c: c.start)


def candidates_from_telemetry(
    cfg: Config,
    session: telemetry.Session,
    offset: float,
    duration: float,
    menus: list[tuple[float, float]] | None = None,
) -> list[Candidate]:
    curve = telemetry.SpeedCurve(session.samples, offset)
    if menus is None:
        menus = session.menu_windows(offset)
    facts = _session_facts(cfg, session)
    ignored = set(cfg.selection.ignore_death_causes)
    out: list[Candidate] = []

    for event in session.events:
        if event.name not in cfg.selection.weights:
            continue
        if event.name == "death" and event.get("cause") in ignored:
            continue

        scene = _scene_at(session, event.t)
        if not _is_playable(cfg, scene):
            continue

        # The clip is built around its trigger and the writer is handed that
        # event's own data, so a near miss that was not a dodge cannot be one:
        # filtering it out of the surrounding events was not enough, it came
        # back through the trigger and became the subject of the whole clip.
        if event.name == "projectile_near_miss" and not is_dodge(cfg, session, event):
            continue

        endless = _is_endless(cfg, scene)
        at = event.t + offset
        window = _clamp(
            cfg,
            *_window(cfg, event, offset, session, endless=endless),
            duration,
            bounds=_level_bounds(session, offset, at),
            attempt=_attempt_bounds(session, offset, at),
            at=at,
            menus=menus,
            min_duration=cfg.clip.endless_min_duration if endless else None,
        )
        if window is None:
            continue

        # Nobody was playing. Measured across the catalogue the idle share sits
        # under 45% for every clip worth having and above 80% for the two that
        # were recorded while the player had walked away, so the gap is wide
        # and the threshold sits in the middle of it.
        if _idle_fraction(
            session, offset, window[0], window[1], menus,
            cfg.selection.idle_speed,
        ) > cfg.selection.max_idle_fraction:
            continue

        # A window that has turned into a retry montage is not worth scoring.
        if sum(
            1
            for e in session.events
            if e.name == "death" and window[0] <= e.t + offset <= window[1]
        ) > cfg.selection.max_deaths_per_clip:
            continue

        score, context = _score(cfg, event, window, session, curve, offset, facts)
        out.append(Candidate(window[0], window[1], score, event.name, context))

    return _cap_per_event(cfg, out, duration)


def clip_budget(cfg: Config, duration: float) -> int:
    """
    How many clips one recording is allowed to yield.

    A flat cap per video is the wrong shape: it gave a twenty-two minute session
    the same six clips as a five minute one, so the longer the recording the
    more of it went unused — measured at one clip every 3.7 minutes against one
    every 1.2. The budget scales with what was actually recorded, with a floor
    so a short session still produces something and a ceiling so one very long
    session cannot fill the whole queue by itself.
    """
    minutes = max(0.0, duration) / 60.0
    target = round(minutes / max(0.5, cfg.selection.minutes_per_clip))
    return int(
        max(cfg.selection.min_clips_per_video, min(cfg.selection.max_clips_per_video, target))
    )


def _cap_per_event(cfg: Config, cands: list[Candidate], duration: float) -> list[Candidate]:
    """
    Keeps only the strongest N candidates of the capped event types.

    Some events are common by nature and cannot be thresholded down to
    interesting. A wall near miss inside 0.7 units at speed 18 is a real squeeze
    by any measure, and one session still produced 59 of them in 12 minutes
    while another produced 2 in 8 — the difference is level geometry and how the
    player was playing, not how close they came.

    So closeness is judged per session rather than against a fixed bar: the
    tightest two passes of a run are the interesting ones whatever the absolute
    numbers were. Filtering here rather than in the game also means the log
    keeps everything, and changing your mind costs a re-select instead of a
    rebuild and a re-record.
    """
    caps = cfg.selection.max_per_event or {}
    if not caps:
        return cands

    kept: list[Candidate] = []
    by_event: dict[str, list[Candidate]] = {}
    for cand in cands:
        if cand.trigger in caps:
            by_event.setdefault(cand.trigger, []).append(cand)
        else:
            kept.append(cand)

    # The per-event caps scale with the recording for the same reason the video
    # budget does: two speed peaks is a sensible ration for five minutes of
    # footage and an arbitrary one for twenty-five.
    scale = max(1.0, (max(0.0, duration) / 60.0) / cfg.selection.cap_scale_minutes)

    for name, group in by_event.items():
        group.sort(key=lambda c: c.score, reverse=True)
        kept.extend(group[: max(0, round(int(caps[name]) * scale))])

    return kept


def candidates_from_signals(cfg: Config, path: Path, duration: float) -> list[Candidate]:
    """Fallback for footage with no matching telemetry session."""
    peaks = analysis.audio_peaks(path, ffmpeg_bin=cfg.ffmpeg)
    out: list[Candidate] = []

    for peak in peaks:
        window = _clamp(
            cfg,
            peak.time - cfg.clip.lead_in,
            peak.time + cfg.clip.lead_out,
            duration,
        )
        if window is None:
            continue
        out.append(
            Candidate(
                window[0],
                window[1],
                # Deliberately below telemetry-derived scores: a loud moment is
                # a much weaker signal than a known game event.
                score=min(peak.prominence, 6.0),
                trigger="audio_peak",
                context={"trigger": "audio_peak", "prominence": round(peak.prominence, 2)},
            )
        )

    return out


def _source_menus(
    source: Any, session: telemetry.Session, offset: float
) -> list[tuple[float, float]]:
    """Menu stretches, measured against the picture when that was possible."""
    try:
        stored = source["menu_windows"]
    except (IndexError, KeyError):
        stored = None
    if stored:
        try:
            return [(float(a), float(b)) for a, b in json.loads(stored)]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return session.menu_windows(offset)


def select_for_source(cfg: Config, source: Any, *, verbose: bool = True) -> list[Candidate]:
    path = Path(source["path"])
    duration = float(source["duration"])

    session = None
    if source["telemetry_path"]:
        session_path = Path(source["telemetry_path"])
        if session_path.exists():
            session = telemetry.load_session(session_path)

    if session and source["offset_seconds"] is not None:
        cands = candidates_from_telemetry(
            cfg, session, float(source["offset_seconds"]), duration,
            menus=_source_menus(source, session, float(source["offset_seconds"])),
        )
        source_kind = "telemetry"
    else:
        cands = candidates_from_signals(cfg, path, duration)
        source_kind = "audio"

    chosen = _dedupe(
        cands,
        cfg.selection.min_gap_between_clips,
        clip_budget(cfg, duration),
        spot_radius=cfg.selection.same_spot_radius,
    )

    if verbose:
        print(
            f"  {path.name}: {len(cands)} candidates from {source_kind} "
            f"-> {len(chosen)} clips"
        )
    return chosen


def _duplicates_existing(conn, cfg: Config, cand: Candidate) -> bool:
    """Whether a clip from this spot in this level is already in the database."""
    radius = cfg.selection.same_spot_radius
    if radius <= 0:
        return False

    spot = _spot(cand)
    if spot is None:
        return False

    for row in conn.execute("SELECT context FROM clips"):
        try:
            context = json.loads(row["context"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        other = _spot(Candidate(0.0, 0.0, 0.0, "", context))
        if other and other[0] == spot[0] and math.dist(other[1:], spot[1:]) <= radius:
            return True
    return False


def select(cfg: Config, *, verbose: bool = True) -> int:
    """Selects clips for every source that has not been through selection yet."""
    total = 0
    with db.connect(cfg.paths.database) as conn:
        sources = conn.execute("SELECT * FROM sources WHERE state = 'new'").fetchall()

        for source in sources:
            try:
                chosen = select_for_source(cfg, source, verbose=verbose)
            except Exception as exc:
                print(f"  !! {Path(source['path']).name}: {exc}")
                continue

            for cand in chosen:
                # Same-spot rejection again, this time across recordings. The
                # per-source pass cannot see that last week's session already
                # produced a clip from this exact corner, and the feed does not
                # care which file a duplicate came from.
                if _duplicates_existing(conn, cfg, cand):
                    if verbose:
                        print(f"    -- skipped {cand.trigger}: same spot as an existing clip")
                    continue

                db.add_clip(
                    conn,
                    source_id=source["id"],
                    start_s=cand.start,
                    end_s=cand.end,
                    score=cand.score,
                    trigger_event=cand.trigger,
                    context=cand.context,
                )
                total += 1

            db.set_state(conn, "sources", source["id"], "selected")

    return total
