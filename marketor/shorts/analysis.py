"""
Signal-based highlight hints, used when telemetry is missing or thin.

Telemetry is always the better source; this exists so that footage recorded
without the game's log (old captures, someone else's playthrough) still yields
something usable rather than nothing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .ffmpeg import FFmpegError, require

# Analysis only needs the envelope, not fidelity. 8 kHz mono keeps a 30 minute
# recording under 30 MB in memory.
_SAMPLE_RATE = 8000


@dataclass
class AudioPeak:
    time: float
    #: Loudness relative to the recording's own median, so the threshold does
    #: not depend on how hot the capture was.
    prominence: float


def audio_envelope(
    path: Path, *, ffmpeg_bin: str = "ffmpeg", window: float = 0.25
) -> tuple[np.ndarray, np.ndarray]:
    """Returns (times, rms) sampled every `window` seconds."""
    cmd = [
        require(ffmpeg_bin),
        "-v", "error",
        "-i", str(path),
        "-vn",
        "-ac", "1",
        "-ar", str(_SAMPLE_RATE),
        "-f", "s16le",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=3600)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-10:]
        raise FFmpegError("audio extraction failed:\n" + "\n".join(tail))

    if not proc.stdout:
        return np.array([]), np.array([])

    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0

    frame = max(1, int(_SAMPLE_RATE * window))
    usable = (len(pcm) // frame) * frame
    if usable == 0:
        return np.array([]), np.array([])

    frames = pcm[:usable].reshape(-1, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    times = np.arange(len(rms)) * window
    return times, rms


def audio_peaks(
    path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    window: float = 0.25,
    min_prominence: float = 2.2,
    min_gap: float = 8.0,
) -> list[AudioPeak]:
    """
    Moments markedly louder than the recording's baseline.

    In this game that mostly catches deaths, explosions and the worm, which is
    exactly the set worth clipping.
    """
    times, rms = audio_envelope(path, ffmpeg_bin=ffmpeg_bin, window=window)
    if len(rms) == 0:
        return []

    baseline = float(np.median(rms))
    if baseline <= 1e-6:
        return []

    prominence = rms / baseline
    candidates = np.where(prominence >= min_prominence)[0]

    peaks: list[AudioPeak] = []
    for idx in candidates:
        t = float(times[idx])
        if peaks and t - peaks[-1].time < min_gap:
            # Keep the loudest moment in each cluster rather than the first.
            if prominence[idx] > peaks[-1].prominence:
                peaks[-1] = AudioPeak(t, float(prominence[idx]))
            continue
        peaks.append(AudioPeak(t, float(prominence[idx])))

    return peaks


def scene_cuts(path: Path, *, threshold: float = 30.0) -> list[float]:
    """
    Scene-change timestamps, or an empty list if PySceneDetect is unavailable.

    Optional on purpose: it is the weakest of the three signals and the slowest,
    so the pipeline must not hard-depend on it.
    """
    try:
        from scenedetect import ContentDetector, detect
    except ImportError:
        return []

    try:
        scenes = detect(str(path), ContentDetector(threshold=threshold))
    except Exception:
        return []

    return [scene[0].get_seconds() for scene in scenes]
