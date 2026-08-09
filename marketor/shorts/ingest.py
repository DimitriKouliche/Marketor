"""
Registers gameplay recordings and pins them to the right telemetry session.

The only hard problem here is time alignment. The recorder and the game are
separate processes, so the pipeline has to work out what wall-clock instant the
first video frame corresponds to. Everything downstream depends on that number
being right, which is why the method used is stored alongside the result.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, ffmpeg, telemetry
from .config import Config

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".flv", ".avi"}

# OBS default filename formats, e.g. "2026-08-06 14-32-11.mkv" or
# "Replay 2026-08-06_14-32-11.mp4".
_FILENAME_TIME = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[ _T-]+(?P<h>\d{2})[-:](?P<mi>\d{2})[-:](?P<s>\d{2})"
)


@dataclass
class Alignment:
    start_utc: datetime
    #: How the value was obtained, kept for debugging a misaligned clip later.
    method: str
    #: Rough worst-case error in seconds.
    confidence: float


def video_start_utc(
    path: Path, info: ffmpeg.MediaInfo, *, tz_offset_minutes: int | None = None
) -> Alignment:
    """
    Best estimate of the wall-clock time of the recording's first frame.

    Tried in order of trustworthiness. The filename wins because OBS stamps it
    at the moment recording starts, which is exactly the instant wanted; file
    mtime is last because it marks the end of the recording and has to be walked
    backwards by the duration.

    `tz_offset_minutes` is the offset the recorder was running at, taken from
    the telemetry session. The filename is local time and the log is UTC, so
    something has to bridge them — and assuming *this* machine's offset is
    wrong for any recording made anywhere else. A clip from a player seven
    hours away lands seven hours off, matches no session at all, and silently
    falls back to audio-peak selection. Falls back to the local offset when the
    session predates this being recorded.
    """
    match = _FILENAME_TIME.search(path.stem)
    if match:
        g = match.groupdict()
        local = datetime(
            int(g["y"]), int(g["mo"]), int(g["d"]),
            int(g["h"]), int(g["mi"]), int(g["s"]),
        )
        if tz_offset_minutes is None:
            return Alignment(
                local.astimezone().astimezone(timezone.utc), "filename-localtz", 1.0
            )

        tz = timezone(timedelta(minutes=tz_offset_minutes))
        return Alignment(
            local.replace(tzinfo=tz).astimezone(timezone.utc), "filename", 1.0
        )

    if info.creation_time:
        return Alignment(info.creation_time, "container-metadata", 2.0)

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return Alignment(mtime - timedelta(seconds=info.duration), "mtime-minus-duration", 5.0)


def pair_with_session(
    path: Path, info: ffmpeg.MediaInfo, sessions: list[telemetry.Session]
) -> tuple[telemetry.Session | None, Alignment]:
    """
    Finds the session this recording belongs to, and when it started.

    Chicken and egg: the filename can only be read in the recorder's timezone,
    which is only known from the session — and the session is found by
    comparing times. So each candidate is tried in its own timezone and the one
    that overlaps most wins. A handful of sessions makes this free, and it is
    the only version that works when the recording came from another machine.
    """
    best: tuple[telemetry.Session | None, Alignment, float] = (
        None, video_start_utc(path, info), 0.0
    )

    for session in sessions:
        if not session.events:
            continue

        alignment = video_start_utc(
            path, info, tz_offset_minutes=session.tz_offset_minutes
        )
        end = alignment.start_utc + timedelta(seconds=info.duration)
        overlap = (
            min(end, session.end_utc) - max(alignment.start_utc, session.start_utc)
        ).total_seconds()

        if overlap > best[2]:
            best = (session, alignment, overlap)

    return best[0], best[1]


def refine_with_sync_marker(
    session: telemetry.Session, start_utc: datetime
) -> tuple[float, str] | None:
    """
    Uses a manual F9 sync marker if one was pressed during the recording.

    Only useful when the operator deliberately pressed it, but when they did it
    is frame-accurate, so it beats every heuristic above.
    """
    markers = session.of_type("sync_marker")
    if not markers:
        return None

    marker = markers[0]
    video_time = (marker.utc - start_utc).total_seconds()
    if video_time < 0:
        return None

    # offset maps telemetry session-seconds -> video seconds
    return video_time - marker.t, "sync-marker"


_LUMA_RE = re.compile(r"pts_time:([\d.]+)|YAVG=([\d.]+)")

#: How far either side of a predicted menu to look for the real one. Wall-clock
#: alignment is good to about a second; this leaves room and no more, so the
#: search cannot wander onto a different menu.
_MENU_SEARCH = 2.0
#: How much bigger the opening frame's brightness step must be than a typical
#: step nearby, before it is believed. The measured ratio is around 45x.
_STEP_RATIO = 8.0


def _menu_frame(samples: list[tuple[float, float]], around: float) -> float | None:
    """
    The exact time a menu opened, searched near where telemetry says it did.

    Opening a menu dims the level and lays two lit cards over it, which changes
    mean frame brightness by more than anything in normal play — measured at 45x
    the surrounding frame-to-frame variation. Detecting that one step is far
    more reliable than trying to recognise the frozen stretch that follows: the
    cards animate, so "frozen" is only frozen to within about 0.05.

    Searching near the predicted time rather than across the whole recording is
    what makes a single global threshold unnecessary.
    """
    window = [
        (t, y) for t, y in samples if abs(t - around) <= _MENU_SEARCH
    ]
    if len(window) < 30:
        return None

    steps = [
        (abs(cur - prev), t)
        for (_, prev), (t, cur) in zip(window, window[1:])
    ]
    if not steps:
        return None

    biggest, when = max(steps)
    typical = statistics.median(size for size, _ in steps)

    if typical <= 0 or biggest < typical * _STEP_RATIO:
        return None
    return when


def refine_with_menus(
    cfg: Config, path: Path, session: telemetry.Session, offset: float
) -> tuple[float, str] | None:
    """
    Corrects the offset by lining menu events up with the picture freezing.

    Wall-clock alignment is only as good as the recorder's filename, which is
    whole seconds. That error is invisible in the tracking crop, which moves
    smoothly, and glaring at a menu, where the frame cuts to letterbox in one
    frame — measured on one capture the letterbox was arriving 1.2s late.

    A full-screen menu sets timeScale to 0, so the picture stops dead on the
    same frame `menu_open` is written: mean brightness steps once and then sits
    flat to three decimal places. That step is frame-accurate and needs no
    knowledge of the game, which makes the two clocks directly comparable.

    ffmpeg's own `freezedetect` was tried first and is not usable here — it
    reports its start about a second into the frozen stretch, which corrected
    the offset the wrong way.

    The median across several menus is what gets applied, so one mis-paired
    boundary cannot drag a whole recording.
    """
    opens = [e.t + offset for e in session.of_type("menu_open")]
    if len(opens) < 3:
        return None

    try:
        output = ffmpeg.run_capture_stderr(
            cfg.ffmpeg,
            ["-hide_banner", "-i", str(path),
             "-vf", "format=gray,signalstats,"
                    "metadata=print:key=lavfi.signalstats.YAVG",
             "-map", "0:v", "-f", "null", "-"],
        )
    except Exception:
        return None

    samples: list[tuple[float, float]] = []
    pending: float | None = None
    for when, value in _LUMA_RE.findall(output):
        if when:
            pending = float(when)
        elif pending is not None:
            samples.append((pending, float(value)))
            pending = None

    if len(samples) < 100:
        return None

    deltas = []
    for predicted in opens:
        actual = _menu_frame(samples, predicted)
        if actual is not None:
            deltas.append(actual - predicted)

    if len(deltas) < 3:
        return None

    correction = statistics.median(deltas)
    if abs(correction) < 0.05:
        return None

    return offset + correction, f"menu-freeze ({correction:+.2f}s, n={len(deltas)})"


def discover(cfg: Config) -> list[Path]:
    root = cfg.paths.recordings
    if not root.exists():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
    )


def ingest(cfg: Config, *, verbose: bool = True) -> list[int]:
    """
    Registers every new recording found under the recordings directory.

    Returns the ids of newly added sources; already-known files are skipped.
    """
    cfg.paths.ensure()
    sessions = telemetry.find_sessions(cfg.paths.telemetry)
    added: list[int] = []

    with db.connect(cfg.paths.database) as conn:
        for path in discover(cfg):
            try:
                info = ffmpeg.probe(path, ffprobe=cfg.ffprobe)
            except ffmpeg.FFmpegError as exc:
                if verbose:
                    print(f"  skip {path.name}: {exc}")
                continue

            if info.duration <= 0:
                if verbose:
                    print(f"  skip {path.name}: zero duration")
                continue

            session, alignment = pair_with_session(path, info, sessions)

            offset: float | None = None
            method = alignment.method
            session_path = None

            if session is not None:
                session_path = str(session.path)
                # Telemetry t is seconds since the game started; video time is
                # seconds since recording started. This offset bridges them.
                offset = (session.start_utc - alignment.start_utc).total_seconds() - session.events[0].t

                refined = refine_with_sync_marker(session, alignment.start_utc)
                if refined:
                    offset, method = refined
                else:
                    # No manual marker; see whether the game's own menus can
                    # pin the two clocks together instead.
                    refined = refine_with_menus(cfg, path, session, offset)
                    if refined:
                        offset, method = refined

            source_id = db.add_source(
                conn,
                path=str(path),
                duration=info.duration,
                width=info.width,
                height=info.height,
                start_utc=alignment.start_utc.isoformat(),
                telemetry_path=session_path,
                offset_seconds=offset,
                alignment=method,
            )

            if source_id is None:
                continue

            added.append(source_id)
            if verbose:
                if session_path:
                    print(
                        f"  + {path.name} ({info.duration:.0f}s) "
                        f"-> {Path(session_path).name} via {method}, offset {offset:+.2f}s"
                    )
                else:
                    print(
                        f"  + {path.name} ({info.duration:.0f}s) "
                        f"-> no telemetry session overlaps; will fall back to audio/scene detection"
                    )

    return added
