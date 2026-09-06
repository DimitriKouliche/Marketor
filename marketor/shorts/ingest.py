"""
Registers gameplay recordings and pins them to the right telemetry session.

The only hard problem here is time alignment. The recorder and the game are
separate processes, so the pipeline has to work out what wall-clock instant the
first video frame corresponds to. Everything downstream depends on that number
being right, which is why the method used is stored alongside the result.
"""

from __future__ import annotations

import json
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
_MENU_SEARCH = 3.5
#: How much bigger the opening frame's brightness step must be than a typical
#: step nearby, before it is believed. The measured ratio is around 45x.
#: A frozen frame differs from the previous one by essentially nothing;
#: normal play measures 2 to 10 on the same scale.
_FREEZE_RATIO = 0.25
_FREEZE_FLOOR = 0.6
_FREEZE_MIN_FRAMES = 8

#: Two power-up cards chosen back to back unfreeze briefly between them.
_MENU_GAP = 0.6


def _freeze_span(
    samples: list[tuple[float, float]], around: float
) -> tuple[float, float] | None:
    """
    The exact time a menu opened, searched near where telemetry says it did.

    `samples` is a per-frame measure of how much the picture changed since the
    previous frame. A full-screen menu sets timeScale to 0, so that measure
    collapses to near zero and stays there until the menu closes — an
    unambiguous stretch with a sharp leading edge.

    An earlier version looked for the step in mean brightness instead. It does
    not work: opening and closing a menu each produce a step, a menu lasts about
    as long as the search window, and the brightness oscillates across the
    transition rather than stepping cleanly, so a rise and a fall land within a
    tenth of a second of each other. Measured on one recording, half the menus
    matched their own opening and half matched a closing, giving two clusters
    1.4s wide and 2.8s apart. The median of that is between the two and correct
    for neither, which is how the letterbox ended up arriving 1.3s late.
    """
    window = [(t, d) for t, d in samples if abs(t - around) <= _MENU_SEARCH]
    if len(window) < 20:
        return None

    moving = sorted(d for _, d in window)
    typical = moving[len(moving) // 2]
    if typical <= 0:
        return None

    # Frozen means far below what the surrounding footage does frame to frame.
    quiet = max(_FREEZE_FLOOR, typical * _FREEZE_RATIO)

    best_start = best_end = None
    best_len = 0
    run_start = run_end = None
    run_len = 0
    for t, d in window:
        if d < quiet:
            if run_start is None:
                run_start = t
            run_end = t
            run_len += 1
        else:
            if run_start is not None and run_len > best_len:
                best_start, best_end, best_len = run_start, run_end, run_len
            run_start = run_end = None
            run_len = 0
    if run_start is not None and run_len > best_len:
        best_start, best_end, best_len = run_start, run_end, run_len

    # A menu is seconds long. A couple of quiet frames is the player standing
    # still, and matching on those would be worse than not matching at all.
    if best_start is None or best_len < _FREEZE_MIN_FRAMES:
        return None
    return best_start, best_end


def _menu_frame(samples: list[tuple[float, float]], around: float) -> float | None:
    """Just the leading edge, which is what the offset correction compares."""
    span = _freeze_span(samples, around)
    return span[0] if span else None


def motion_series(cfg: Config, path: Path) -> list[tuple[float, float]]:
    """
    Per-frame "how much did the picture change", for the whole recording.

    One ffmpeg pass, shared by everything that needs to know when the game was
    frozen. Downscaled and decimated on purpose: this measures movement, which
    needs neither resolution nor every frame, and at full size it was the
    slowest step in ingestion by a wide margin.
    """
    try:
        output = ffmpeg.run_capture_stderr(
            cfg.ffmpeg,
            ["-hide_banner", "-i", str(path),
             "-vf", "format=gray,scale=160:-2,fps=20,"
                    "tblend=all_mode=difference,signalstats,"
                    "metadata=print:key=lavfi.signalstats.YAVG",
             "-map", "0:v", "-f", "null", "-"],
        )
    except Exception:
        return []

    samples: list[tuple[float, float]] = []
    pending: float | None = None
    for when, value in _LUMA_RE.findall(output):
        if when:
            pending = float(when)
        elif pending is not None:
            samples.append((pending, float(value)))
            pending = None

    return samples if len(samples) >= 100 else []


def _frozen_runs(
    motion: list[tuple[float, float]], quiet: float
) -> list[tuple[float, float]]:
    """Every stretch where the picture stopped moving."""
    runs: list[tuple[float, float]] = []
    start = end = None
    count = 0
    for t, d in motion:
        if d < quiet:
            if start is None:
                start = t
            end = t
            count += 1
        else:
            if start is not None and count >= _FREEZE_MIN_FRAMES:
                runs.append((start, end))
            start = end = None
            count = 0
    if start is not None and count >= _FREEZE_MIN_FRAMES:
        runs.append((start, end))
    return runs


def measure_menu_windows(
    session: telemetry.Session, offset: float, motion: list[tuple[float, float]]
) -> list[tuple[float, float]] | None:
    """
    The menu stretches as the picture actually shows them, not as logged.

    The game brackets a menu by the screen object being active, which turns on a
    moment before play stops and animates out after it resumes, so the logged
    window ran up to 1.8s past the end of the freeze and the letterbox sat over
    live gameplay.

    One logged window is not always one menu, either. Collecting two power-ups
    in quick succession leaves the screen active throughout while the picture
    unfreezes between the two choices, and the game writes it as a single menu.
    Measuring one freeze per logged window therefore letterboxed the first card
    and dropped the second — worse than the loose window it replaced. So every
    freeze inside the logged span is kept, and only the sub-second gap between
    two cards is bridged.

    Returns None when too few could be located, so the caller keeps the logged
    windows rather than a half-measured mixture.
    """
    logged: list[tuple[float, float]] = []
    opened: float | None = None
    for event in session.events:
        if event.name == "menu_open":
            opened = event.t + offset
        elif event.name == "menu_close" and opened is not None:
            logged.append((opened, event.t + offset))
            opened = None

    if not logged or not motion:
        return None

    # A telemetry session routinely runs longer than the recording that was
    # made during it — the player starts the game, then starts capturing — so
    # menus from before the recording began sit at negative video times. They
    # cannot be located in a video that does not contain them, and counting
    # them as failures threw away four good measurements out of four because
    # eleven others happened off-camera.
    span_end = motion[-1][0]
    logged = [(a, b) for a, b in logged if b > 0.0 and a < span_end]
    if not logged:
        return None

    values = sorted(d for _, d in motion)
    typical = values[len(values) // 2]
    if typical <= 0:
        return None
    runs = _frozen_runs(motion, max(_FREEZE_FLOOR, typical * _FREEZE_RATIO))

    measured: list[tuple[float, float]] = []
    located = 0
    for begin, finish in logged:
        inside = [
            (a, b) for a, b in runs
            if b > begin - _MENU_SEARCH and a < finish + _MENU_SEARCH
        ]
        if not inside:
            continue
        located += 1

        # Two cards in a row are one menu to the viewer: the picture blinks
        # between them and the letterbox should not.
        merged: list[tuple[float, float]] = []
        for a, b in inside:
            if merged and a - merged[-1][1] <= _MENU_GAP:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        measured.extend(merged)

    if located < max(1, len(logged) // 2):
        return None
    return measured


def refine_with_menus(
    cfg: Config,
    path: Path,
    session: telemetry.Session,
    offset: float,
    motion: list[tuple[float, float]] | None = None,
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

    samples = motion if motion is not None else motion_series(cfg, path)
    if not samples:
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

    archive = cfg.paths.archive.resolve()
    found = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        # Belt and braces: the archive is meant to be a sibling, but if someone
        # points it inside `recordings` the recursive walk would hand every
        # finished recording straight back.
        if archive in path.resolve().parents:
            continue
        found.append(path)
    return sorted(found)


def archive_processed(cfg: Config, *, verbose: bool = True) -> int:
    """
    Moves recordings whose clips are all rendered into the archive directory.

    The database already declines to ingest a path it has seen, but that promise
    only lasts as long as the database — and `regen --full` deletes it. Moving
    the file puts the same guarantee on disk, where a wipe cannot reach it.

    The stored path is updated as it moves, so rendering and any later `regen`
    still find the source. Returns how many were archived.
    """
    moved = 0
    destination = cfg.paths.archive

    with db.connect(cfg.paths.database) as conn:
        sources = conn.execute("SELECT id, path FROM sources").fetchall()

        for source in sources:
            path = Path(source["path"])
            if not path.exists() or path.parent.resolve() == destination.resolve():
                continue

            # "Done" means every clip cut from it has reached a terminal state.
            # A source still mid-render must stay put: moving it would break the
            # very step that needs it.
            pending = conn.execute(
                """
                SELECT COUNT(*) n FROM clips
                 WHERE source_id = ?
                   AND state IN ('selected', 'written', 'rendered')
                """,
                (source["id"],),
            ).fetchone()["n"]
            if pending:
                continue

            total = conn.execute(
                "SELECT COUNT(*) n FROM clips WHERE source_id = ?", (source["id"],)
            ).fetchone()["n"]
            if not total:
                continue  # nothing was ever cut from it; leave it to be retried

            destination.mkdir(parents=True, exist_ok=True)
            target = destination / path.name
            if target.exists():
                target = destination / f"{path.stem}-{source['id']}{path.suffix}"

            try:
                path.rename(target)
            except OSError as exc:
                # Locked by a player or an antivirus scan; it will be retried
                # on the next run rather than failing the pipeline.
                if verbose:
                    print(f"  !! {path.name} non deplace ({exc.strerror})")
                continue

            conn.execute(
                "UPDATE sources SET path = ? WHERE id = ?", (str(target), source["id"])
            )
            moved += 1
            if verbose:
                print(f"  -> archive {path.name}")

    return moved


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
            menu_windows: str | None = None
            motion: list[tuple[float, float]] = []

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
                    # pin the two clocks together instead. One pass over the
                    # video serves both this and the menu window measurement.
                    motion = motion_series(cfg, path)
                    refined = refine_with_menus(cfg, path, session, offset, motion=motion)
                    if refined:
                        offset, method = refined

                windows = measure_menu_windows(session, offset, motion)
                if windows:
                    menu_windows = json.dumps(
                        [[round(a, 3), round(b, 3)] for a, b in windows]
                    )

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
                menu_windows=menu_windows,
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
