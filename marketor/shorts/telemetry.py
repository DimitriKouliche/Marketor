"""
Reads the JSONL event log written by the game's Telemetry class.

The log is the whole reason this pipeline can pick good clips: it says exactly
what happened and when, so nothing has to be inferred from pixels.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Event:
    #: Seconds since the game process started. Monotonic, unaffected by pausing.
    t: float
    #: Wall clock, which is what aligns the log to a separately recorded video.
    utc: datetime
    name: str
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass
class Session:
    path: Path
    events: list[Event]

    @property
    def start_utc(self) -> datetime | None:
        return self.events[0].utc if self.events else None

    @property
    def end_utc(self) -> datetime | None:
        return self.events[-1].utc if self.events else None

    def of_type(self, *names: str) -> list[Event]:
        wanted = set(names)
        return [e for e in self.events if e.name in wanted]

    @property
    def samples(self) -> list[Event]:
        """The 5 Hz movement snapshots, used for speed and framing."""
        return self.of_type("sample")

    def covers(self, when: datetime) -> bool:
        if not self.events:
            return False
        return self.start_utc <= when <= self.end_utc

    @property
    def tz_offset_minutes(self) -> int | None:
        """
        The recorder's UTC offset at the time of the session.

        Needed because recorders name files in local time while every timestamp
        in here is UTC. Absent on sessions logged before this was recorded.
        """
        for event in self.events:
            if event.name == "session_start":
                value = event.get("tz_offset_minutes")
                return int(value) if isinstance(value, (int, float)) else None
            # session_start is the first line; no point walking the whole file.
            break
        return None

    def menu_windows(self, offset: float) -> list[tuple[float, float]]:
        """
        Stretches paused on a full-screen menu, in video-time seconds.

        Returned as (start, end) pairs so the renderer can show the whole 16:9
        frame there instead of cropping it — a power-up screen spans the full
        width and is unreadable in a 9:16 crop.
        """
        windows: list[tuple[float, float]] = []
        opened: float | None = None

        for event in self.events:
            if event.name == "menu_open":
                # Two opens in a row means the close was lost with a truncated
                # write or a scene change. Keep the later one.
                opened = event.t + offset
            elif event.name == "menu_close" and opened is not None:
                windows.append((opened, event.t + offset))
                opened = None

        return windows


def _parse_utc(raw: str) -> datetime:
    # Unity writes a trailing Z; fromisoformat only learned to accept that in
    # 3.11, and the project targets 3.10.
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_session(path: Path) -> Session:
    """
    Parses one session log.

    Malformed trailing lines are expected and skipped: the writer buffers, so a
    crash or a hard kill truncates the final record.
    """
    events: list[Event] = []
    # utf-8-sig, not utf-8: Unity's StreamWriter emits a BOM, which otherwise
    # attaches to the first line and makes it the one record that never parses.
    # Every session was silently losing its session_start.
    with path.open("r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                events.append(
                    Event(
                        t=float(raw["t"]),
                        utc=_parse_utc(raw["utc"]),
                        name=str(raw["event"]),
                        data=raw.get("data") or {},
                    )
                )
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    events.sort(key=lambda e: e.t)
    _discount_paused_time(events)
    return Session(path=path, events=events)


#: Durations the game measured on an unscaled clock. The power-up screen freezes
#: play with timeScale = 0, so a menu opened mid-action was counted as part of
#: the action: a two second wall-run with a choice in the middle was logged as
#: twenty, and the copy writer duly reported twenty seconds of wall-running.
#: Fixed in the game, but every recording made before that carries the inflated
#: numbers, and those recordings are not going to be made again.
_PAUSED_DURATION_FIELDS = {
    "wallrun": "duration",
    "bonus_lost": "held_seconds",
}


def _discount_paused_time(events: list[Event]) -> None:
    """Subtracts menu time from durations that were measured through it."""
    pauses: list[tuple[float, float]] = []
    opened: float | None = None
    for event in events:
        if event.name == "menu_open":
            opened = event.t
        elif event.name == "menu_close" and opened is not None:
            pauses.append((opened, event.t))
            opened = None

    if not pauses:
        return

    for event in events:
        field = _PAUSED_DURATION_FIELDS.get(event.name)
        if field is None:
            continue
        try:
            duration = float(event.data.get(field))
        except (TypeError, ValueError):
            continue
        if duration <= 0:
            continue

        # The event is logged when the action ends, so it spans back from there.
        began = event.t - duration
        paused = sum(
            max(0.0, min(event.t, b) - max(began, a)) for a, b in pauses
        )
        if paused > 0.05:
            event.data[field] = round(max(0.0, duration - paused), 3)


def find_sessions(directory: Path) -> list[Session]:
    if not directory.exists():
        return []
    sessions = [load_session(p) for p in sorted(directory.glob("session-*.jsonl"))]
    return [s for s in sessions if s.events]


def session_covering(sessions: Iterable[Session], start: datetime, end: datetime) -> Session | None:
    """
    Picks the session that overlaps a recording's time range the most.

    Recording and playing are separate processes started at different moments,
    so an exact match never happens; the largest overlap is the right answer.
    """
    best: Session | None = None
    best_overlap = 0.0

    for s in sessions:
        if not s.events:
            continue
        overlap = (min(end, s.end_utc) - max(start, s.start_utc)).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = s, overlap

    return best


class SpeedCurve:
    """
    Player speed and on-screen position over time, queryable by video second.

    Built once per source and reused for both scoring and framing.
    """

    def __init__(self, samples: list[Event], offset: float):
        #: Video-time seconds for each sample.
        self.times: list[float] = []
        self.speeds: list[float] = []
        self.viewport_x: list[float] = []

        for e in samples:
            self.times.append(e.t + offset)
            self.speeds.append(float(e.get("speed", 0.0) or 0.0))

            vp = e.get("viewport") or [-1.0, -1.0]
            # -1 means there was no main camera that frame.
            self.viewport_x.append(float(vp[0]) if vp and vp[0] >= 0 else -1.0)

    def __len__(self) -> int:
        return len(self.times)

    def _slice(self, start: float, end: float) -> slice:
        lo = bisect_left(self.times, start)
        hi = bisect_left(self.times, end)
        return slice(lo, hi)

    def mean_speed(self, start: float, end: float) -> float:
        window = self.speeds[self._slice(start, end)]
        return sum(window) / len(window) if window else 0.0

    def peak_speed(self, start: float, end: float) -> float:
        window = self.speeds[self._slice(start, end)]
        return max(window) if window else 0.0

    def viewport_series(self, start: float, end: float) -> list[tuple[float, float]]:
        """
        On-screen player position over a window, as (seconds-from-start, x).

        Feeds the tracking crop. Samples with no camera are dropped rather than
        interpolated, so a gap leaves the crop where it was instead of lurching
        to a bogus position.
        """
        window = self._slice(start, end)
        out: list[tuple[float, float]] = []

        for t, x in zip(self.times[window], self.viewport_x[window]):
            if x >= 0:
                out.append((t - start, x))
        return out

    def median_viewport_x(self, start: float, end: float) -> float | None:
        """
        Where the player sat horizontally on screen, 0..1, across the window.

        The median rather than the mean: a single frame where the camera has not
        caught up yet should not drag the crop off the action.
        """
        window = [x for x in self.viewport_x[self._slice(start, end)] if x >= 0]
        if not window:
            return None
        window.sort()
        return window[len(window) // 2]
