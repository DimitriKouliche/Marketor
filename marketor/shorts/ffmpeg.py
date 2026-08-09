"""
Thin wrapper over the ffmpeg/ffprobe binaries.

Everything the pipeline does to video goes through here so there is one place
that knows how the tools are invoked and how their failures are reported.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


class FFmpegError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    creation_time: datetime | None


# Where ffmpeg ends up when installed the usual ways on Windows. Checked when
# it is not on PATH, which happens constantly: winget appends to the user PATH,
# and every shell opened before the install still has the old copy.
_SEARCH_GLOBS = (
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\**\bin",
    r"%ProgramFiles%\ffmpeg\bin",
    r"%ProgramData%\chocolatey\bin",
    r"C:\ffmpeg\bin",
)

_resolved: dict[str, str] = {}


def _search(binary: str) -> str | None:
    """Looks for the binary in the usual install locations."""
    import glob

    exe = binary if binary.lower().endswith(".exe") else f"{binary}.exe"

    for pattern in _SEARCH_GLOBS:
        expanded = os.path.expandvars(pattern)
        if "%" in expanded:  # a variable that does not exist on this machine
            continue
        for directory in glob.glob(expanded, recursive=True):
            candidate = Path(directory) / exe
            if candidate.is_file():
                return str(candidate)
    return None


def require(binary: str) -> str:
    """
    Absolute path to an ffmpeg-family binary.

    An absolute path in shorts.yaml wins, then PATH, then the known install
    locations. Results are cached: the glob is cheap but runs per clip.
    """
    if binary in _resolved:
        return _resolved[binary]

    found = shutil.which(binary) or _search(binary)
    if not found:
        raise FFmpegError(
            f"{binary!r} not found on PATH or in the usual install locations.\n"
            "    Install it with:  winget install Gyan.FFmpeg\n"
            "    Already installed? Open a NEW terminal — winget updates PATH\n"
            "    only for shells started after the install. Or set an absolute\n"
            f"    path for '{binary}' in shorts.yaml."
        )

    _resolved[binary] = found
    return found


def run(binary: str, args: Sequence[str], *, timeout: int = 3600) -> str:
    """Runs ffmpeg/ffprobe and returns stdout, raising with stderr on failure."""
    cmd = [require(binary), *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        # ffmpeg puts everything on stderr, including the actual reason.
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        raise FFmpegError(f"{binary} failed ({proc.returncode}):\n{tail}")
    return proc.stdout


def probe(path: Path, *, ffprobe: str = "ffprobe") -> MediaInfo:
    out = run(
        ffprobe,
        [
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
    )
    data: dict[str, Any] = json.loads(out)

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"{path.name} has no video stream")

    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))

    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)

    created = None
    raw_created = (fmt.get("tags") or {}).get("creation_time")
    if raw_created:
        try:
            created = datetime.fromisoformat(
                raw_created.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError:
            created = None

    return MediaInfo(
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"),
        has_audio=has_audio,
        creation_time=created,
    )


def _parse_fps(raw: str) -> float:
    try:
        num, _, den = raw.partition("/")
        denominator = float(den) if den else 1.0
        return float(num) / denominator if denominator else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def run_capture_stderr(binary: str, args: Sequence[str], *, timeout: int = 3600) -> str:
    """
    Runs ffmpeg and returns stderr regardless of exit code.

    Analysis filters like freezedetect report their findings to stderr as log
    lines, so the normal `run` — which discards stderr unless the command fails
    — cannot see them.
    """
    proc = subprocess.run(
        [require(binary), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.stderr or ""
