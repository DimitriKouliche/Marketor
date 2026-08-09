"""
Cuts, reframes and captions a clip into a finished vertical video.

Reframing is the interesting part. The game is 16:9 and the output is 9:16, so
roughly two thirds of every frame has to go. Because the telemetry records where
the player was on screen, the crop can be centred on the action instead of on
the middle of the frame, which is where a naive converter throws the player out
of shot entirely.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import captions, db, ffmpeg, telemetry
from .config import Config


def _escape_filter_path(path: Path) -> str:
    r"""
    Escapes a Windows path for use inside an ffmpeg filter argument.

    ffmpeg parses filter arguments itself, so C:\x becomes an escape sequence
    and the drive colon separates options. Forward slashes plus an escaped colon
    is the form that survives both layers.
    """
    text = str(path.resolve()).replace("\\", "/")
    return text.replace(":", "\\:").replace("'", r"\'")


def crop_x(
    source_width: int, crop_width: int, viewport_x: float | None, damping: float
) -> int:
    """
    Horizontal position of the crop window, in source pixels.

    Damping pulls the window back toward centre so the framing does not lurch
    with every jump; at 0 the crop is always centred, at 1 it follows exactly.
    """
    slack = source_width - crop_width
    if slack <= 0:
        return 0

    centre = slack / 2.0
    if viewport_x is None:
        return int(round(centre))

    desired = viewport_x * source_width - crop_width / 2.0
    damped = centre + (desired - centre) * damping
    return int(round(max(0.0, min(float(slack), damped))))


def track_keyframes(
    series: list[tuple[float, float]],
    *,
    duration: float,
    source_width: int,
    crop_width: int,
    damping: float,
    deadzone: float = 0.35,
    rate: float = 30.0,
    smooth_seconds: float = 1.5,
    max_pan_px_per_s: float = 160.0,
    follow_gain: float = 0.18,
    ease_seconds: float = 0.6,
) -> list[tuple[float, int]]:
    """
    Turns raw player positions into a crop path that holds still and then glides.

    The naive version — point the crop at the smoothed player position every
    frame — measured 733px of camera travel to achieve 23px of net movement.
    In a game built on bouncing, continuously centring the player just mirrors
    the bounce, and the whole shot vibrates.

    So the camera is a follower with a deadzone, the way a human operator works:
    hold still while the subject moves around the middle of frame, and only pan
    once they actually threaten to leave it. Smoothing kills sample noise before
    it reaches the follower, the gain eases each correction instead of snapping,
    and the velocity clamp stops a fast traversal whipping the shot across.

    Crucially `rate` must be the output frame rate. sendcmd applies each change
    as a discrete jump, so updating at 10 Hz on 30 fps video holds three frames
    still and then lurches — read as a judder far worse than the wobble it was
    meant to fix. One keyframe per frame turns the same movement into motion.
    """
    if not series:
        return []

    steps = max(2, int(duration * rate) + 1)
    times = [i / rate for i in range(steps)]

    # Nearest-sample resample. Interpolating across a gap would invent motion
    # that never happened.
    resampled: list[float] = []
    cursor = 0
    for t in times:
        while cursor + 1 < len(series) and abs(series[cursor + 1][0] - t) <= abs(series[cursor][0] - t):
            cursor += 1
        resampled.append(series[cursor][1])

    half = max(1, int(smooth_seconds * rate / 2))
    smoothed: list[float] = []
    for i in range(len(resampled)):
        lo, hi = max(0, i - half), min(len(resampled), i + half + 1)
        window = resampled[lo:hi]
        smoothed.append(sum(window) / len(window))

    slack = float(max(0, source_width - crop_width))
    max_step = max_pan_px_per_s / rate
    limit = deadzone * (crop_width / 2.0)

    # Start framed on the median position rather than on whatever the first
    # sample happened to be, so the clip opens composed.
    ordered = sorted(smoothed)
    current = float(crop_x(source_width, crop_width, ordered[len(ordered) // 2], damping))

    path: list[float] = []
    for vx in smoothed:
        player_px = vx * source_width
        error = player_px - (current + crop_width / 2.0)

        # Inside the deadzone the camera simply does not move.
        if abs(error) > limit:
            overshoot = error - math.copysign(limit, error)
            step = max(-max_step, min(max_step, overshoot * follow_gain))
            current = max(0.0, min(slack, current + step))

        path.append(current)

    # A deadzone follower starts and stops abruptly: it sits still, then moves
    # off at full speed the instant the player crosses the boundary. Averaging
    # the resulting path rounds those corners into an eased move, which is the
    # difference between a camera pan and a camera flinch.
    ease = max(1, int(ease_seconds * rate))
    eased: list[float] = []
    for i in range(len(path)):
        lo, hi = max(0, i - ease // 2), min(len(path), i + ease // 2 + 1)
        window = path[lo:hi]
        eased.append(sum(window) / len(window))

    keyframes: list[tuple[float, int]] = []
    emitted: int | None = None

    for t, x in zip(times, eased):
        rounded = int(round(x))
        # Emit on every change, however small. Batching small moves into larger
        # ones is exactly what produces a visible lurch.
        if emitted is None or rounded != emitted:
            keyframes.append((t, rounded))
            emitted = rounded

    return keyframes


def write_track_commands(keyframes: list[tuple[float, int]], path: Path) -> Path:
    """Writes an ffmpeg sendcmd script that pans the crop window."""
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    last: int | None = None
    for t, x in keyframes:
        # Only emit changes; a static stretch needs no commands.
        if x != last:
            lines.append(f"{t:.3f} crop x {x};")
            last = x

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_filter(
    cfg: Config,
    *,
    source_width: int,
    source_height: int,
    viewport_x: float | None,
    subtitle_path: Path | None,
    track_path: Path | None = None,
    initial_x: int | None = None,
    letterbox: list[tuple[float, float]] | None = None,
) -> str:
    """Assembles the -filter_complex chain, ending in a [v] output label."""
    out_w, out_h = cfg.clip.width, cfg.clip.height
    parts: list[str] = []
    last = "v0"

    if cfg.clip.reframe == "crop":
        # Widest 9:16 window that fits in the source height.
        crop_w = min(source_width, int(round(source_height * out_w / out_h)))
        crop_w -= crop_w % 2

        # The crop reads the whole frame, so it has to come off a split rather
        # than straight from the input when a letterboxed copy is also wanted.
        source = "[main]" if letterbox else "[0:v]"
        if letterbox:
            parts.append("[0:v]split=2[main][whole]")

        if track_path is not None:
            # sendcmd drives crop's x over time; it must sit ahead of crop in
            # the chain to deliver the commands.
            x = initial_x if initial_x is not None else 0
            parts.append(
                f"{source}sendcmd=f='{_escape_filter_path(track_path)}',"
                f"crop={crop_w}:{source_height}:{x}:0,"
                f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v0]"
            )
        else:
            x = crop_x(source_width, crop_w, viewport_x, cfg.clip.track_damping)
            parts.append(
                f"{source}crop={crop_w}:{source_height}:{x}:0,"
                f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v0]"
            )

        if letterbox:
            # The menu frame, centred in black bars, laid over the crop only
            # while a menu is up. A power-up screen puts two cards and their
            # descriptions across the full width; cropped to 9:16 the choice is
            # unreadable, and reading it is the entire point of the game
            # pausing.
            #
            # Not quite the whole frame: the cards only reach 57% of the width
            # in practice, and their layout roots bound them at 70%, so the
            # outer edge is cave and HUD. Trimming it before the fit makes the
            # text 1.4x bigger in the same bars, which is the difference
            # between readable and technically visible on a phone.
            zoom = min(1.0, max(0.3, cfg.render.letterbox_zoom))
            wide = int(round(source_width * zoom))
            wide -= wide % 2
            parts.append(
                f"[whole]crop={wide}:{source_height}:(iw-{wide})/2:0,"
                f"scale={out_w}:-2:flags=lanczos,"
                f"pad={out_w}:{out_h}:0:(oh-ih)/2:color=black,setsar=1[vwhole]"
            )
            windows = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in letterbox)
            parts.append(f"[v0][vwhole]overlay=0:0:enable='{windows}'[vc]")
            last = "vc"
    else:
        # Whole frame over a blurred, zoomed copy of itself. Loses nothing, but
        # the gameplay ends up small, so it is the fallback rather than default.
        parts.append("[0:v]split=2[bg][fg]")
        parts.append(
            f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},gblur=sigma=28[bgb]"
        )
        parts.append(f"[fg]scale={out_w}:-2:flags=lanczos[fgs]")
        parts.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")

    if subtitle_path is not None:
        parts.append(f"[{last}]subtitles='{_escape_filter_path(subtitle_path)}'[v]")
    else:
        parts.append(f"[{last}]null[v]")

    return ";".join(parts)


def render_clip(
    cfg: Config,
    *,
    source_path: Path,
    start: float,
    end: float,
    hook: str,
    out_path: Path,
    viewport_x: float | None = None,
    viewport_series: list[tuple[float, float]] | None = None,
    beats: list[dict] | None = None,
    source_info: ffmpeg.MediaInfo | None = None,
    menu_windows: list[tuple[float, float]] | None = None,
) -> Path:
    info = source_info or ffmpeg.probe(source_path, ffprobe=cfg.ffprobe)
    duration = end - start

    letterbox = _clip_windows(menu_windows, start, duration, cfg.render.letterbox_pad)

    subtitle_path = None
    # Timed beats when the writer produced them; a single hook otherwise.
    cues = (
        captions.beat_cues(
            beats,
            duration,
            min_hold=cfg.render.caption_min_seconds,
            max_hold=cfg.render.caption_max_seconds,
        )
        if beats
        else []
    )
    if not cues:
        cues = captions.hook_cues(hook, duration)
    if cues:
        subtitle_path = out_path.with_suffix(".ass")
        captions.write(cfg, cues, subtitle_path)

    # A static crop is right on average and wrong at the extremes: during a long
    # traversal the player ends up pinned to the frame edge. With enough samples
    # the window follows instead.
    track_path = None
    initial_x = None
    if (
        cfg.clip.reframe == "crop"
        and cfg.clip.tracking
        and viewport_series
        and len(viewport_series) >= 4
    ):
        crop_w = min(info.width, int(round(info.height * cfg.clip.width / cfg.clip.height)))
        crop_w -= crop_w % 2
        keyframes = track_keyframes(
            viewport_series,
            duration=duration,
            source_width=info.width,
            crop_width=crop_w,
            damping=cfg.clip.track_damping,
            deadzone=cfg.clip.track_deadzone,
            smooth_seconds=cfg.clip.track_smoothing,
            # One keyframe per output frame, so a pan is motion rather than a
            # sequence of held frames punctuated by jumps.
            rate=float(cfg.clip.fps),
        )
        if keyframes:
            initial_x = keyframes[0][1]
            track_path = write_track_commands(keyframes, out_path.with_suffix(".track.txt"))

    filter_complex = build_filter(
        cfg,
        source_width=info.width,
        source_height=info.height,
        viewport_x=viewport_x,
        subtitle_path=subtitle_path,
        track_path=track_path,
        initial_x=initial_x,
        letterbox=letterbox,
    )

    args = [
        "-y",
        # Seek before -i so ffmpeg skips to the region instead of decoding from
        # the start of what may be a 40 minute file.
        "-ss", f"{start:.3f}",
        "-i", str(source_path),
        "-t", f"{duration:.3f}",
        "-filter_complex", filter_complex,
        "-map", "[v]",
    ]

    if info.has_audio:
        args += [
            "-map", "0:a:0",
            "-af", f"loudnorm=I={cfg.clip.lufs}:TP=-1.5:LRA=11",
            "-c:a", "aac",
            "-b:a", "160k",
            "-ar", "48000",
        ]
    else:
        args += ["-an"]

    args += [
        "-c:v", "libx264",
        "-profile:v", "high",
        "-crf", str(cfg.render.crf),
        "-preset", cfg.render.preset,
        "-pix_fmt", "yuv420p",
        "-r", str(cfg.clip.fps),
        # Every platform streams these; without faststart the moov atom lands at
        # the end and some uploaders reject the file outright.
        "-movflags", "+faststart",
        str(out_path),
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(cfg.ffmpeg, args)
    return out_path


def _clip_windows(
    windows: list[tuple[float, float]] | None,
    start: float,
    duration: float,
    pad: float,
) -> list[tuple[float, float]]:
    """
    Converts video-time windows to clip-relative, keeping only what overlaps.

    Padded on both sides because the telemetry marks when the screen object
    toggles, while the screen itself fades. Without the pad the switch lands a
    few frames early and flashes a cropped menu.
    """
    result: list[tuple[float, float]] = []
    for begin, finish in windows or []:
        a = max(0.0, begin - start - pad)
        b = min(duration, finish - start + pad)
        if b - a > 0.05:
            result.append((a, b))

    # Overlapping windows would double-count in the enable expression. Harmless
    # arithmetically — the sum is still non-zero — but merging keeps the
    # expression short when a clip catches several menus back to back.
    merged: list[tuple[float, float]] = []
    for a, b in sorted(result):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _menu_windows_for_source(source: Any) -> list[tuple[float, float]]:
    """Video-time stretches where the game sat on a full-screen menu."""
    if not source["telemetry_path"] or source["offset_seconds"] is None:
        return []

    session_path = Path(source["telemetry_path"])
    if not session_path.exists():
        return []

    return telemetry.load_session(session_path).menu_windows(
        float(source["offset_seconds"])
    )


def _viewport_for_clip(
    source: Any, start: float, end: float
) -> tuple[float | None, list[tuple[float, float]]]:
    """On-screen player position across the clip: (median, full series)."""
    if not source["telemetry_path"] or source["offset_seconds"] is None:
        return None, []

    session_path = Path(source["telemetry_path"])
    if not session_path.exists():
        return None, []

    session = telemetry.load_session(session_path)
    curve = telemetry.SpeedCurve(session.samples, float(source["offset_seconds"]))
    return curve.median_viewport_x(start, end), curve.viewport_series(start, end)


def render_pending(cfg: Config, *, limit: int | None = None, verbose: bool = True) -> int:
    """Renders every clip that has copy written but no video yet."""
    cfg.paths.ensure()
    rendered = 0

    with db.connect(cfg.paths.database) as conn:
        clips = db.clips_in_state(conn, "written")
        if limit:
            clips = clips[:limit]

        for clip in clips:
            source = db.source_for_clip(conn, clip["id"])
            if source is None:
                continue

            source_path = Path(source["path"])
            if not source_path.exists():
                print(f"  !! source missing for clip {clip['id']}: {source_path}")
                db.set_state(conn, "clips", clip["id"], "failed")
                continue

            out_path = cfg.paths.output / f"clip_{clip['id']:05d}.mp4"
            start, end = float(clip["start_s"]), float(clip["end_s"])

            median_x, series = _viewport_for_clip(source, start, end)
            menus = _menu_windows_for_source(source)

            try:
                beats = json.loads(clip["beats"] or "[]")
            except (json.JSONDecodeError, TypeError):
                beats = []

            try:
                render_clip(
                    cfg,
                    source_path=source_path,
                    start=start,
                    end=end,
                    hook=clip["hook"] or "",
                    out_path=out_path,
                    viewport_x=median_x,
                    viewport_series=series,
                    beats=beats,
                    menu_windows=menus,
                )
            except ffmpeg.FFmpegError as exc:
                print(f"  !! clip {clip['id']} failed to render: {exc}")
                db.set_state(conn, "clips", clip["id"], "failed")
                continue

            conn.execute(
                "UPDATE clips SET render_path = ?, state = 'rendered' WHERE id = ?",
                (str(out_path), clip["id"]),
            )
            rendered += 1
            if verbose:
                print(f"  + clip {clip['id']} -> {out_path.name} ({end - start:.1f}s)")

    return rendered
