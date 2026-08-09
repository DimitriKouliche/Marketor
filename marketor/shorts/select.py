"""
Picks which moments become clips.

This is the stage that decides whether the output is worth posting. A clip is
built around one triggering event, widened to include its run-up, then scored on
how much else was happening around it.
"""

from __future__ import annotations

import json
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


def _clamp(cfg: Config, start: float, end: float, duration: float) -> tuple[float, float] | None:
    """Fits a window inside the recording and inside the allowed clip length."""
    start = max(0.0, start)
    end = min(duration, end)
    if end <= start:
        return None

    length = end - start
    if length > cfg.clip.max_duration:
        # Trim from the front: the payoff is at the end of the window.
        start = end - cfg.clip.max_duration
    elif length < cfg.clip.min_duration:
        needed = cfg.clip.min_duration - length
        start = max(0.0, start - needed)
        end = min(duration, start + cfg.clip.min_duration)

    if end - start < cfg.clip.min_duration:
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
        "summary": {
            "session_minutes": round(span / 60.0, 1),
            "total_deaths": len(deaths),
            "levels_cleared": len(victories),
            "deaths_by_cause": dict(sorted(causes.items(), key=lambda kv: -kv[1])[:5]),
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
    density_mult = 1.0 + min(len(nearby), 8) * 0.06

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
    elif event.name == "input_flurry":
        presses = float(event.get("presses", 0) or 0)
        intensity = 1.0 + max(0.0, presses - 9.0) * 0.08
    elif event.name == "ghost_lead_change":
        # Taking the lead is the moment worth watching; losing it is setup.
        intensity = 1.4 if event.get("leader") == "player" else 1.0
    elif event.name == "wallrun":
        intensity = 1.0 + min(float(event.get("duration", 1.0) or 1.0), 8.0) * 0.1

    score = base * speed_mult * density_mult * intensity

    scene = _scene_at(session, event.t)

    trigger_data = dict(event.data)
    if event.name in ("death", "shield_hit") and "cause" in trigger_data:
        trigger_data["cause"] = humanise_cause(trigger_data.get("cause"))

    context = {
        "trigger": event.name,
        "trigger_data": trigger_data,
        "scene": scene,
        "peak_speed": round(peak, 1),
        "mean_speed": round(curve.mean_speed(start, end), 1),
        "nearby_events": [
            {"name": e.name, "at": round(e.t + offset - start, 2), "data": e.data}
            for e in nearby
        ],
        "session": facts["summary"],
        "level": _clip_facts(session, facts, event, scene),
    }
    return score, context


def _dedupe(cands: list[Candidate], min_gap: float, limit: int) -> list[Candidate]:
    """
    Greedy highest-score-first selection with a spacing rule.

    Two clips cut from the same ten seconds of footage read as duplicates on a
    feed even when the triggering events differ, so spacing is enforced against
    everything already accepted rather than only the previous pick.
    """
    chosen: list[Candidate] = []
    for cand in sorted(cands, key=lambda c: c.score, reverse=True):
        if any(cand.overlaps(kept, min_gap) for kept in chosen):
            continue
        chosen.append(cand)
        if len(chosen) >= limit:
            break
    return sorted(chosen, key=lambda c: c.start)


def candidates_from_telemetry(
    cfg: Config, session: telemetry.Session, offset: float, duration: float
) -> list[Candidate]:
    curve = telemetry.SpeedCurve(session.samples, offset)
    facts = _session_facts(cfg, session)
    ignored = set(cfg.selection.ignore_death_causes)
    out: list[Candidate] = []

    for event in session.events:
        if event.name not in cfg.selection.weights:
            continue
        if event.name == "death" and event.get("cause") in ignored:
            continue

        window = _clamp(cfg, *_window(cfg, event, offset, session), duration)
        if window is None:
            continue

        score, context = _score(cfg, event, window, session, curve, offset, facts)
        out.append(Candidate(window[0], window[1], score, event.name, context))

    return _cap_per_event(cfg, out)


def _cap_per_event(cfg: Config, cands: list[Candidate]) -> list[Candidate]:
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

    for name, group in by_event.items():
        group.sort(key=lambda c: c.score, reverse=True)
        kept.extend(group[: max(0, int(caps[name]))])

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
            cfg, session, float(source["offset_seconds"]), duration
        )
        source_kind = "telemetry"
    else:
        cands = candidates_from_signals(cfg, path, duration)
        source_kind = "audio"

    chosen = _dedupe(cands, cfg.selection.min_gap_between_clips, cfg.selection.max_clips_per_video)

    if verbose:
        print(
            f"  {path.name}: {len(cands)} candidates from {source_kind} "
            f"-> {len(chosen)} clips"
        )
    return chosen


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
