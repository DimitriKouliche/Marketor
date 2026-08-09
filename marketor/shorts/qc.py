"""
Automated checks on a rendered clip before it reaches the approval queue.

These catch the failures that are embarrassing rather than interesting: a clip
that opens on ten black frames, one that came out silent, one whose loudness is
so far off that the platform's own normalisation will mangle it. Judgement about
whether a clip is *good* stays with the human reviewer.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import db, ffmpeg
from .config import Config


@dataclass
class QCResult:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    measurements: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "passed": self.passed,
                "checks": self.checks,
                "notes": self.notes,
                "measurements": self.measurements,
            }
        )


def _integrated_loudness(cfg: Config, path: Path) -> float | None:
    """Measured LUFS, or None if the file has no audio to measure."""
    try:
        proc = subprocess.run(
            [
                ffmpeg.require(cfg.ffmpeg),
                "-nostats",
                "-i", str(path),
                "-af", "ebur128=framelog=verbose",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
    except (ffmpeg.FFmpegError, subprocess.TimeoutExpired):
        return None

    # The summary block at the end of stderr carries the integrated value.
    matches = re.findall(r"I:\s*(-?\d+\.?\d*)\s*LUFS", proc.stderr or "")
    return float(matches[-1]) if matches else None


def _leading_black(cfg: Config, path: Path) -> float:
    """Seconds of black at the very start of the clip."""
    try:
        proc = subprocess.run(
            [
                ffmpeg.require(cfg.ffmpeg),
                "-i", str(path),
                "-vf", "blackdetect=d=0.1:pic_th=0.98",
                "-an", "-f", "null", "-",
            ],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
    except (ffmpeg.FFmpegError, subprocess.TimeoutExpired):
        return 0.0

    for match in re.finditer(
        r"black_start:(\d+\.?\d*) black_end:(\d+\.?\d*)", proc.stderr or ""
    ):
        start, end = float(match.group(1)), float(match.group(2))
        # Only a run that begins at the very start matters; a black frame in
        # the middle is usually a legitimate scene transition.
        if start <= 0.05:
            return end
    return 0.0


def check(cfg: Config, path: Path) -> QCResult:
    """Runs the automated checks."""
    limit = cfg.clip.max_duration
    result = QCResult(passed=True)

    if not path.exists() or path.stat().st_size == 0:
        return QCResult(passed=False, checks={"file_exists": False},
                        notes=[f"{path.name} is missing or empty"])

    try:
        info = ffmpeg.probe(path, ffprobe=cfg.ffprobe)
    except ffmpeg.FFmpegError as exc:
        return QCResult(passed=False, checks={"probe": False}, notes=[str(exc)])

    result.checks["file_exists"] = True
    result.checks["probe"] = True
    result.measurements["duration"] = round(info.duration, 2)

    duration_ok = cfg.clip.min_duration - 1 <= info.duration <= limit + 1
    result.checks["duration"] = duration_ok
    if not duration_ok:
        result.notes.append(
            f"duration {info.duration:.1f}s outside {cfg.clip.min_duration}-{limit}s"
        )

    resolution_ok = info.width == cfg.clip.width and info.height == cfg.clip.height
    result.checks["resolution"] = resolution_ok
    if not resolution_ok:
        result.notes.append(
            f"resolution {info.width}x{info.height}, expected "
            f"{cfg.clip.width}x{cfg.clip.height}"
        )

    result.checks["has_audio"] = info.has_audio
    if not info.has_audio:
        result.notes.append("clip has no audio track")

    if info.has_audio:
        lufs = _integrated_loudness(cfg, path)
        if lufs is not None:
            result.measurements["lufs"] = round(lufs, 1)
            # Three LU of slack: platform normalisation handles small drift,
            # but a clip that is 10 LU quiet reads as broken.
            loudness_ok = abs(lufs - cfg.clip.lufs) <= 3.0
            result.checks["loudness"] = loudness_ok
            if not loudness_ok:
                result.notes.append(
                    f"loudness {lufs:.1f} LUFS, target {cfg.clip.lufs}"
                )

    black = _leading_black(cfg, path)
    result.measurements["leading_black"] = round(black, 2)
    black_ok = black < 0.5
    result.checks["no_leading_black"] = black_ok
    if not black_ok:
        result.notes.append(f"{black:.1f}s of black at the start")

    result.passed = all(result.checks.values())
    return result


def check_pending(cfg: Config, *, verbose: bool = True) -> tuple[int, int]:
    """Runs QC over every rendered clip. Returns (passed, failed)."""
    passed = failed = 0

    with db.connect(cfg.paths.database) as conn:
        for clip in db.clips_in_state(conn, "rendered"):
            if not clip["render_path"]:
                continue

            result = check(cfg, Path(clip["render_path"]))
            state = "ready" if result.passed else "qc_failed"

            conn.execute(
                "UPDATE clips SET qc = ?, state = ? WHERE id = ?",
                (result.to_json(), state, clip["id"]),
            )

            if result.passed:
                passed += 1
                if verbose:
                    print(f"  ok clip {clip['id']}")
            else:
                failed += 1
                if verbose:
                    print(f"  !! clip {clip['id']}: {'; '.join(result.notes)}")

    return passed, failed
