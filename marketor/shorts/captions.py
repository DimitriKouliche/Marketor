"""
Builds the burned-in caption track as an ASS subtitle file.

ASS rather than ffmpeg's drawtext because it gives real outlines, fades and
per-line positioning for free. That difference is most of what separates a
caption that looks native to the platform from one that looks pasted on.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

from .config import Config

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},2,8,60,60,{margin_top},1
Style: Tag,{font},{tag_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{tag_outline},2,2,60,60,{margin_bottom},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class CaptionCue:
    text: str
    start: float
    end: float
    style: str = "Hook"


def _timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    # ASS uses centiseconds and a single-digit hour field.
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _escape(text: str) -> str:
    """Neutralises the ASS override-block and line-break syntax."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", " ")
        .strip()
    )


def _wrap(text: str, width: int = 34) -> str:
    """
    Hard-wraps to short lines.

    Vertical video is narrow, and a hook that wraps on its own lands wherever
    the renderer decides. Choosing the break points keeps the shape readable.

    34 characters is what fits across 1080px inside the margins at Impact 64px,
    with a little room to spare. It was 24 when the type was 96px. Wrapping
    narrower than the type allows just adds lines, and every extra line is
    gameplay the clip does not get back.
    """
    lines = textwrap.wrap(text, width=width, break_long_words=False) or [text]
    return "\\N".join(lines)


def build(cfg: Config, cues: list[CaptionCue]) -> str:
    r = cfg.render
    height = cfg.clip.height

    header = _HEADER.format(
        width=cfg.clip.width,
        height=height,
        font=r.font_name,
        size=r.font_size,
        tag_size=int(r.font_size * 0.55),
        outline=r.outline,
        tag_outline=max(2, r.outline - 2),
        margin_top=int(height * r.safe_top),
        margin_bottom=int(height * r.safe_bottom),
    )

    lines = [header]
    for cue in cues:
        # Fade plus a short rise into place. A line that simply appears
        # reads as a subtitle track; one that arrives reads as something a
        # person made. The move is over in 180ms — felt, but nobody waits
        # for it before reading.
        margin = int(height * r.safe_top)
        rise = int(height * 0.012)
        cx = cfg.clip.width // 2
        move = (
            r"{\fad(140,160)\move(%d,%d,%d,%d,0,180)}"
            % (cx, margin + rise, cx, margin)
        )
        text = move + _wrap(_escape(cue.text))
        lines.append(
            f"Dialogue: 0,{_timestamp(cue.start)},{_timestamp(cue.end)},"
            f"{cue.style},,0,0,0,,{text}"
        )

    return "\n".join(lines) + "\n"


def hook_cues(hook: str, duration: float, *, hold: float = 3.6) -> list[CaptionCue]:
    """
    Fallback caption plan: one hook, on screen at the start.

    It leaves after a few seconds rather than staying for the whole clip so it
    never covers the payoff it was written to set up.
    """
    if not hook:
        return []
    return [CaptionCue(text=hook, start=0.25, end=min(hold, max(2.0, duration - 1.0)))]


# Defaults; the real values come from RenderConfig at call time.
MIN_HOLD = 5.0
MAX_HOLD = 9.0

# Rough reading pace for burned-in caption text, in seconds per character.
# Deliberately slower than silent-reading speed: the viewer is also watching
# the gameplay, and a caption they only half-read is worse than one line fewer.
SECONDS_PER_CHAR = 0.045


def _hold_needed(text: str, min_hold: float = MIN_HOLD) -> float:
    """Minimum time this line must stay up to be readable."""
    return max(min_hold, len(text) * SECONDS_PER_CHAR)


def _fit(
    beats: list[tuple[float, str]], duration: float, min_hold: float
) -> list[tuple[float, str]] | None:
    """
    Places beats so every one is on screen long enough to read.

    Two passes, because one is not enough. Going forwards guarantees no beat
    replaces its predecessor early; going backwards guarantees the last one
    finishes before the clip does. The backward pass has to cascade — pulling
    the closing line earlier is useless if the beat ahead of it still holds its
    original slot, which is exactly the bug this replaced.

    Returns None when the beats cannot all fit, so the caller can drop one.
    """
    placed = list(beats)

    for i in range(1, len(placed)):
        earliest = placed[i - 1][0] + _hold_needed(placed[i - 1][1], min_hold)
        if placed[i][0] < earliest:
            placed[i] = (earliest, placed[i][1])

    latest_end = duration - 0.2
    for i in reversed(range(len(placed))):
        at, text = placed[i]
        latest_start = latest_end - _hold_needed(text, min_hold)
        if at > latest_start:
            at = latest_start
            placed[i] = (at, text)
        latest_end = at

    if not placed or placed[0][0] < 0.2 - 1e-6:
        return None
    return placed


def beat_cues(
    beats: list[dict],
    duration: float,
    *,
    min_hold: float = MIN_HOLD,
    max_hold: float = MAX_HOLD,
) -> list[CaptionCue]:
    """
    Turns the writer's timed beats into cues, enforcing readable timing.

    The model chooses roughly where each line belongs — placing a punchline on
    the moment it refers to is the whole point — but it has no feel for reading
    speed, so the holds are corrected here. Each beat runs until the next one
    starts, so there is never a gap with nothing on screen mid-thought.
    """
    cleaned: list[tuple[float, str]] = []
    for beat in beats:
        text = str(beat.get("text") or "").strip()
        if not text:
            continue
        try:
            at = float(beat.get("at", 0.0))
        except (TypeError, ValueError):
            at = 0.0
        cleaned.append((max(0.2, min(at, max(0.2, duration - min_hold))), text))

    if not cleaned:
        return []

    cleaned.sort(key=lambda item: item[0])

    # The beats may simply not fit — the writer is told the clip's ceiling but
    # can still misjudge the spacing. Dropping a middle one is the least bad
    # loss: the first establishes context for a stranger and the last is the
    # payoff, while the middle is elaboration.
    spaced = _fit(cleaned, duration, min_hold)
    while spaced is None and len(cleaned) > 1:
        drop = len(cleaned) // 2
        cleaned = cleaned[:drop] + cleaned[drop + 1:]
        spaced = _fit(cleaned, duration, min_hold)

    if spaced is None:
        return []

    cues: list[CaptionCue] = []
    for i, (at, text) in enumerate(spaced):
        if i + 1 < len(spaced):
            end = min(spaced[i + 1][0], at + max(max_hold, _hold_needed(text, min_hold)))
        else:
            # Let the closing line ride out the clip rather than clearing early;
            # it is the line the viewer should be left with.
            end = duration - 0.2
        if end - at >= 0.8:
            cues.append(CaptionCue(text=text, start=at, end=end))

    return cues


def write(cfg: Config, cues: list[CaptionCue], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # libass reads UTF-8; a BOM makes it drop the first style line.
    path.write_text(build(cfg, cues), encoding="utf-8")
    return path
