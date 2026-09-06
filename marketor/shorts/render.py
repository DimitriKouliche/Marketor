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
from dataclasses import dataclass
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


#: Longer than this between two telemetry samples is a dropout, not a
#: sampling interval, and gets held rather than interpolated across.
_MAX_INTERPOLATED_GAP = 0.6


def track_keyframes(
    series: list[tuple[float, float]],
    *,
    duration: float,
    source_width: int,
    crop_width: int,
    damping: float,
    deadzone: float = 0.20,
    rate: float = 30.0,
    smooth_seconds: float = 1.5,
    max_pan_px_per_s: float = 400.0,
    follow_gain: float = 0.18,
    ease_seconds: float = 0.6,
    safe_fraction: float = 0.86,
    rescue_px_per_s: float = 700.0,
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

    # Linear between samples, held across a real gap.
    #
    # The game reports the player about five times a second and the camera runs
    # at sixty, so nearest-sample resampling holds each position for twelve
    # frames. Interpolating is not inventing motion: the player is a
    # continuously moving object, and a straight line between two real
    # positions 200ms apart is closer to the truth than a staircase. A genuine
    # telemetry dropout is different and is still held rather than bridged, so
    # a gap leaves the camera where it was.
    #
    # This was tried as a fix for a visible shimmer and is not one — measured
    # across five clips it moved the worst frame-to-frame velocity change from
    # 3px to 2px, because the smoothing average was already absorbing the
    # staircase. The shimmer was the zoom. Kept because it is the correct
    # reconstruction, not because it solved anything.
    resampled: list[float] = []
    cursor = 0
    for t in times:
        while cursor + 1 < len(series) and series[cursor + 1][0] <= t:
            cursor += 1

        current_t, current_x = series[cursor]
        if cursor + 1 >= len(series):
            resampled.append(current_x)
            continue

        next_t, next_x = series[cursor + 1]
        span = next_t - current_t
        if span <= 0 or span > _MAX_INTERPOLATED_GAP:
            resampled.append(current_x)
            continue

        weight = min(1.0, max(0.0, (t - current_t) / span))
        resampled.append(current_x + (next_x - current_x) * weight)

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

    # Containment. Everything above is a preference about how the camera should
    # feel; this is the one thing that is not negotiable, because a clip where
    # the player is off screen is not a clip.
    #
    # Measured on real footage: the player crosses the frame faster than the pan
    # cap 40% of the time, with a 608px window in a 1920px source. No amount of
    # tuning the follower fixes that — the follower is rate limited by design,
    # and raising the limit far enough to keep up brings back the whip pan the
    # limit exists to prevent.
    #
    # So the smoothed path is kept wherever it already contains the player, and
    # pushed the minimum distance needed wherever it does not. The camera glides
    # when it can and locks to the player when it must, which is what a human
    # operator does with a fast subject.
    #
    # Checked against the raw position, not the smoothed one. Smoothing is a
    # 1.5 second moving average, so at speed it trails the player by most of a
    # second — containing the average keeps the average in frame while the
    # player themself is already outside it. That mistake made this clamp
    # worth 0.1 percentage points instead of the real fix.
    # Rate limited, or containment trades one artefact for a worse one: an
    # unbounded correction produced 725px single-frame jumps, which reads as the
    # camera being yanked. This ceiling is far above the ordinary follower's, so
    # it only engages when the player is genuinely escaping.
    safe = safe_fraction * (crop_width / 2.0)
    rescue_step = rescue_px_per_s / rate
    contained: list[float] = []
    previous: float | None = None

    for vx, x in zip(resampled, eased):
        player_px = vx * source_width
        centre = x + crop_width / 2.0
        if player_px < centre - safe:
            x = player_px + safe - crop_width / 2.0
        elif player_px > centre + safe:
            x = player_px - safe - crop_width / 2.0

        x = max(0.0, min(slack, x))
        if previous is not None:
            delta = x - previous
            if abs(delta) > rescue_step:
                x = previous + math.copysign(rescue_step, delta)
        previous = x
        contained.append(x)

    keyframes: list[tuple[float, int]] = []
    emitted: int | None = None

    for t, x in zip(times, contained):
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


def native_crop_width(source_width: int, source_height: int, cfg: Config) -> int:
    """Widest 9:16 window that fits in the source height."""
    width = min(source_width, int(round(source_height * cfg.clip.width / cfg.clip.height)))
    return width - width % 2


def widened_crop_width(
    series: list[tuple[float, float]], source_width: int, source_height: int, cfg: Config
) -> int:
    """
    Crop width for this clip, widened when the player would not stay inside.

    The 9:16 window is only 608px of a 1080p source, and on the busiest clips
    the player sweeps 1344px of it. No camera path fits a 1344px range into a
    608px window; the follower can only choose which end to lose them at.

    Widening trades resolution for framing: the gameplay renders smaller inside
    black bars, and the player stays visible. Applied per clip and only when the
    measured range demands it, so calm clips keep the full-bleed look.
    """
    native = native_crop_width(source_width, source_height, cfg)
    if not cfg.clip.widen_when_needed or not series:
        return native

    inside = [x for _, x in series if 0.0 <= x <= 1.0]
    if len(inside) < 4:
        return native

    # Ignore the extreme few percent: a single respawn should not letterbox a
    # whole clip that is otherwise well framed.
    inside.sort()
    lo = inside[int(len(inside) * 0.02)]
    hi = inside[int(len(inside) * 0.98)]
    span = (hi - lo) * source_width

    # The follower still contributes; it only has to cover what the window
    # cannot. Anything up to this much travel stays comfortable.
    needed = span * cfg.clip.widen_coverage
    if needed <= native:
        return native

    ceiling = min(source_width, int(source_width * cfg.clip.widen_max_fraction))
    width = min(ceiling, int(round(needed)))
    return max(native, width - width % 2)


def zoom_expression(cfg: Config, duration: float, trigger_at: float | None) -> str | None:
    """
    A time-varying zoom factor, as an ffmpeg expression.

    Two effects share one filter because they are the same operation:

    - A slow push in over the whole clip. A shot that creeps closer reads as
      going somewhere; a static one reads as a recording. The move is a few
      percent, well under the threshold where anyone notices it consciously.
    - A tighter punch on the triggering moment. The telemetry knows exactly
      when the missile passes or the diamond is lost, so the camera can lean in
      just before and release after — reactive editing that no generic tool can
      do, because no generic tool knows what happened.

    Returns None when both are switched off, so the filter stays out of the
    chain entirely rather than multiplying by one.
    """
    push = max(0.0, cfg.clip.zoom_push)
    punch = max(0.0, cfg.clip.zoom_punch)
    if push <= 0 and punch <= 0:
        return None

    terms = ["1"]
    if push > 0 and duration > 0:
        terms.append(f"{push:.4f}*min(it/{duration:.3f},1)")

    if punch > 0 and trigger_at is not None:
        # Gaussian, so the lean in and the release are both smooth and the peak
        # lands on the event rather than around it.
        width = max(0.2, cfg.clip.zoom_punch_seconds)
        terms.append(
            rf"{punch:.4f}*exp(-pow((it-{trigger_at:.3f})/{width:.3f}\,2))"
        )

    return "+".join(terms)


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
    crop_width: int | None = None,
    zoom_expr: str | None = None,
) -> str:
    """Assembles the -filter_complex chain, ending in a [v] output label."""
    out_w, out_h = cfg.clip.width, cfg.clip.height
    parts: list[str] = []
    last = "v0"

    # Dropping the dead frames has to be the first thing that happens, so every
    # filter downstream — sendcmd, the zoom, the subtitles — sees the shortened
    # timeline and needs no adjustment of its own. setpts closes the gaps that
    # select leaves behind.
    # Rate conversion happens once, here, for every path. It used to sit inside
    # the zoom stage, which meant a clip that skipped the zoom — every widened
    # clip — reached the output at source rate and was thinned by -r instead.
    # Two different conversions on two kinds of clip is two kinds of judder.
    parts.append(f"[0:v]fps={cfg.clip.fps}[vin]")
    base = "[vin]"

    if cfg.clip.reframe == "crop":
        crop_w = crop_width or native_crop_width(source_width, source_height, cfg)

        # A window wider than 9:16 cannot fill the frame, so it is fitted and
        # padded instead of stretched. That is the whole trade: the gameplay
        # gets smaller, and the player stops leaving the shot.
        widened = crop_w > native_crop_width(source_width, source_height, cfg)
        # Fitted inside the box rather than scaled to its width. Scaling to
        # width alone assumes the output is taller than what it receives,
        # which holds for every vertical cut and for none of the 4:3 ones —
        # there the scaled frame overflows and pad refuses to shrink it.
        fit = (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            if widened else
            f"scale={out_w}:{out_h}:flags=lanczos,setsar=1"
        )

        # The crop reads the whole frame, so it has to come off a split rather
        # than straight from the input when a letterboxed copy is also wanted.
        source = "[main]" if letterbox else base
        if letterbox:
            parts.append(f"{base}split=2[main][whole]")

        # A widened crop exists because the action already does not fit. Zooming
        # back in would spend exactly what the widening bought, so those clips
        # keep their static framing.
        if widened:
            zoom_expr = None

        # crop cannot carry the zoom: it evaluates w and h once at configuration
        # time and only re-evaluates x and y per frame. zoompan is the filter
        # that takes a time-varying scale factor, and it sits after the fit so
        # the tracking crop still decides where to look.
        stage = "vz" if zoom_expr else "v0"

        if track_path is not None:
            # sendcmd drives crop's x over time; it must sit ahead of crop in
            # the chain to deliver the commands.
            x = initial_x if initial_x is not None else 0
            parts.append(
                f"{source}sendcmd=f='{_escape_filter_path(track_path)}',"
                f"crop={crop_w}:{source_height}:{x}:0,{fit}[{stage}]"
            )
        else:
            x = crop_x(source_width, crop_w, viewport_x, cfg.clip.track_damping)
            parts.append(
                f"{source}crop={crop_w}:{source_height}:{x}:0,{fit}[{stage}]"
            )

        if zoom_expr:
            # zoompan restamps its output at its own fps, so it has to be told
            # the rate the chain is already running at — set it wrong and the
            # clip stretches and plays in slow motion.
            parts.append(
                f"[{stage}]zoompan=z='{zoom_expr}':d=1"
                ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":s={out_w}x{out_h}:fps={cfg.clip.fps}[v0]"
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
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease"
                f":force_divisible_by=2:flags=lanczos,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[vwhole]"
            )
            windows = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in letterbox)
            parts.append(f"[v0][vwhole]overlay=0:0:enable='{windows}'[vc]")
            last = "vc"
    else:
        # Whole frame over a blurred, zoomed copy of itself. Loses nothing, but
        # the gameplay ends up small, so it is the fallback rather than default.
        parts.append(f"{base}split=2[bg][fg]")
        parts.append(
            f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},gblur=sigma=28[bgb]"
        )
        parts.append(f"[fg]scale={out_w}:-2:flags=lanczos[fgs]")
        parts.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")

    # Hold the final frame. A hard cut to black ends the clip; a held frame
    # ends the thought — and on a looping platform it is the moment the last
    # caption is actually read, which is what makes a replay happen.
    if cfg.clip.freeze_end > 0:
        parts.append(
            f"[{last}]tpad=stop_mode=clone:"
            f"stop_duration={cfg.clip.freeze_end:.2f}[vf]"
        )
        last = "vf"

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
    trigger_at: float | None = None,
    captions_on: bool = True,
) -> Path:
    info = source_info or ffmpeg.probe(source_path, ffprobe=cfg.ffprobe)
    duration = end - start
    # `duration` shrinks below if dead air is cut; the input window does not.
    source_duration = duration

    letterbox = _clip_windows(
        menu_windows, start, duration, cfg.render.letterbox_pad,
        lead=cfg.render.letterbox_lead,
        trail=cfg.render.letterbox_trail,
    )

    subtitle_path = None
    # Burned-in text is a feed device: it exists because a muted clip
    # autoplays past a thumb. A clip someone chose to open does not need to
    # be told what it is, and the text sits on top of the gameplay that is
    # the whole reason they opened it.
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
    if not captions_on:
        cues = []
    if cues:
        # Carry the closing line across the held frame. Holding a frame with no
        # caption on it is just a longer clip; holding it under the last line is
        # what gives that line time to land.
        if cfg.clip.freeze_end > 0:
            cues[-1].end += cfg.clip.freeze_end

        subtitle_path = out_path.with_suffix(".ass")
        captions.write(cfg, cues, subtitle_path)

    # A static crop is right on average and wrong at the extremes: during a long
    # traversal the player ends up pinned to the frame edge. With enough samples
    # the window follows instead.
    #
    # Holding one window for the whole clip was tried and rejected. It works on
    # paper — 47 clips of 57 keep the player framed with no camera at all, and
    # the rest do once the window is widened — and it removes the last of the
    # shimmer, 0 direction reversals against 2. But the widening it needs costs
    # letterbox on the clips that move most, and watched side by side the slight
    # movement is the better picture. Kept as a note rather than a setting: the
    # answer here came from looking, not from the numbers.
    track_path = None
    initial_x = None
    crop_w = None
    if (
        cfg.clip.reframe == "crop"
        and cfg.clip.tracking
        and viewport_series
        and len(viewport_series) >= 4
    ):
        crop_w = widened_crop_width(viewport_series, info.width, info.height, cfg)
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
            safe_fraction=cfg.clip.track_safe_fraction,
            rescue_px_per_s=cfg.clip.track_rescue_px_per_s,
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
        crop_width=crop_w if track_path is not None else None,
        zoom_expr=zoom_expression(cfg, duration, trigger_at),
    )

    args = [
        "-y",
        # Seek before -i so ffmpeg skips to the region instead of decoding from
        # the start of what may be a 40 minute file.
        "-ss", f"{start:.3f}",
        # Bound the input, not just the output: tpad only pads at end of
        # stream, so the source has to actually run out for the held frame to
        # exist. The output limit then has to leave room for it.
        "-t", f"{source_duration:.3f}",
        "-i", str(source_path),
        "-t", f"{duration + cfg.clip.freeze_end:.3f}",
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
    lead: float = 0.0,
    trail: float = 0.0,
) -> list[tuple[float, float]]:
    """
    Converts video-time windows to clip-relative, keeping only what overlaps.

    Padded on both sides because the telemetry marks when the screen object
    toggles, while the screen itself fades. Without the pad the switch lands a
    few frames early and flashes a cropped menu.
    """
    result: list[tuple[float, float]] = []
    for begin, finish in windows or []:
        # The lead is one-sided. Measured against the picture, the overlay was
        # landing a tenth of a second after the menu appeared, which reads as
        # late because the eye catches the menu first; arriving a hair early
        # does not read as anything. Padding the tail instead would bring back
        # the fault this replaced, the letterbox sitting over live gameplay.
        # Lead and trail are separate because the menu is not symmetric. It
        # appears in a single frame, so a tenth of a second of lead is enough;
        # it leaves over an animation that runs 0.4s past the picture
        # unfreezing, and cutting there dropped the letterbox while the cards
        # were still on screen.
        a = max(0.0, begin - start - pad - lead)
        b = min(duration, finish - start + pad + trail)
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
    # Measured from the picture when that was possible, which is more accurate
    # than the event log at both edges — see ingest.measure_menu_windows.
    try:
        stored = source["menu_windows"]
    except (IndexError, KeyError):
        stored = None
    if stored:
        try:
            return [(float(a), float(b)) for a, b in json.loads(stored)]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

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

            # Where the payoff sits, so the camera can lean into it.
            try:
                context = json.loads(clip["context"] or "{}")
                trigger_at = context.get("trigger_at")
                trigger_at = float(trigger_at) if trigger_at is not None else None
            except (json.JSONDecodeError, TypeError, ValueError):
                trigger_at = None

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
                    trigger_at=trigger_at,
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
