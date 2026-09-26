"""Building small real media files, and measuring what ended up in them."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

RATE = 16000


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {out.stderr[-600:]}")
    return out


def _stamp(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def srt(path: Path, seconds: float, cues=None) -> Path:
    """Numbered filler lines, or the given (start, end, text) cues."""
    if cues is None:
        cues = [(t, t + 1.5, f"line {i}")
                for i, t in enumerate(range(0, int(seconds), 2), start=1)]
    blocks = [f"{i}\n{_stamp(a)} --> {_stamp(b)}\n{text}\n"
              for i, (a, b, text) in enumerate(cues, start=1)]
    path.write_text("\n".join(blocks), encoding="utf8")
    return path


def make_media(dest: Path, seconds: float = 6.0, audio: Path | None = None,
               channels: int = 2, audio_codec: str = "aac",
               title: str = "Surround", subtitles: bool = True, cues=None) -> Path:
    """A tiny video with one audio track and, by default, a subtitle track.

    The audio is a steady tone unless `audio` names a file to use instead.
    """
    container = dest.suffix.lower().lstrip(".")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}"]
    if audio is None:
        layout = "5.1" if channels == 6 else ("mono" if channels == 1 else "stereo")
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate=48000:duration={seconds},"
                f"aformat=channel_layouts={layout}"]
    else:
        cmd += ["-i", str(audio)]
    if subtitles:
        cmd += ["-i", str(srt(dest.with_suffix(".srt"), seconds, cues))]
    cmd += ["-map", "0:v", "-map", "1:a"]
    if subtitles:
        cmd += ["-map", "2:s"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", audio_codec, "-ac", str(channels), "-ar", "48000",
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", f"title={title}",
            "-disposition:a:0", "default"]
    if subtitles:
        cmd += ["-c:s", "mov_text" if container == "mp4" else "srt",
                "-metadata:s:s:0", "language=eng"]
    cmd += ["-t", str(seconds), str(dest)]
    run(cmd)
    return dest


def streams(path: Path) -> list[dict]:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", str(path)])
    return json.loads(out.stdout)["streams"]


def audio_tags(path: Path, audio_index: int) -> dict:
    audio = [s for s in streams(path) if s["codec_type"] == "audio"]
    return {k.lower(): v for k, v in (audio[audio_index].get("tags") or {}).items()}


def samples(path: Path, audio_index: int) -> np.ndarray:
    """One audio track decoded to mono float samples at RATE."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
         "-map", f"0:a:{audio_index}", "-ac", "1", "-ar", str(RATE),
         "-f", "s16le", "-"], capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode()[-400:])
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float64) / 32768.0


def rms(pcm: np.ndarray, start: float, end: float) -> float:
    lo, hi = int(start * RATE), int(end * RATE)
    chunk = pcm[lo:hi]
    return float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
