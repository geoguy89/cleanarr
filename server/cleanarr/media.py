"""ffmpeg work: what is in a file, and how the cleaned track gets into it.

The original file is never edited in place. Everything is built beside it and
swapped in only once ffprobe agrees the result has every stream the original
had, plus the new one, at the same duration. A media library is not a thing to
be clever with.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

# Thread cap for every ffmpeg call. Audio work is nearly single-threaded
# anyway, and leaving the rest of the cores alone is what keeps Plex playing
# while a file is being cleaned. Set by the pipeline from settings.
THREADS = int(os.environ.get("CLEANARR_FFMPEG_THREADS", "2"))


def _threads() -> list[str]:
    return ["-threads", str(THREADS)] if THREADS else []

CLEAN_TITLE = "Cleaned - English"
# How much a remux is allowed to differ from the original before it is thrown
# away: containers round durations, but a truncated file is a lost episode.
DURATION_TOLERANCE = 1.0


class MediaError(RuntimeError):
    pass


@dataclass
class AudioStream:
    index: int          # index within the file
    audio_index: int    # index among audio streams only (the a:N form)
    codec: str
    channels: int
    sample_rate: int
    language: str
    title: str
    default: bool
    handler: str = ""

    @property
    def is_cleaned(self) -> bool:
        """Is this our track?

        Two fields, because MP4 has no per-track title: ffmpeg accepts
        `-metadata:s:a:N title=` on an MP4 and drops it at mux time. MP4 does
        keep `handler_name`, so the marker is written to both and read from
        either. Untouched tracks carry handler_name "SoundHandler", so matching
        on it cannot claim a track we did not make.
        """
        return CLEAN_TITLE.lower() in (
            self.title.strip().lower(), self.handler.strip().lower())


@dataclass
class Probe:
    path: Path
    duration: float
    video_streams: int
    audio: list[AudioStream]
    container: str

    @property
    def cleaned_track(self) -> AudioStream | None:
        return next((a for a in self.audio if a.is_cleaned), None)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe(path: str | Path) -> Probe:
    path = Path(path)
    out = _run([FFPROBE, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)])
    if out.returncode != 0:
        raise MediaError(f"ffprobe failed on {path.name}: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    audio: list[AudioStream] = []
    ai = 0
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        audio.append(AudioStream(
            index=int(s.get("index", 0)),
            audio_index=ai,
            codec=str(s.get("codec_name", "")),
            channels=int(s.get("channels", 2) or 2),
            sample_rate=int(s.get("sample_rate", 48000) or 48000),
            language=str(tags.get("language", "")),
            title=str(tags.get("title", "")),
            default=bool((s.get("disposition") or {}).get("default")),
            handler=str(tags.get("handler_name", "")),
        ))
        ai += 1
    return Probe(
        path=path,
        duration=float((data.get("format") or {}).get("duration") or 0.0),
        video_streams=sum(1 for s in streams if s.get("codec_type") == "video"),
        audio=audio,
        container=path.suffix.lower().lstrip("."),
    )


def pick_source_track(p: Probe, prefer_language: str = "eng") -> AudioStream:
    """The track to listen to and to base the clean one on.

    Preference order: the default English track, any English track, the
    default track, the first track. A track already titled "Cleaned - English"
    is never used as a source - cleaning a cleaned track would compound the
    muting and drift further from the original every run.
    """
    usable = [a for a in p.audio if not a.is_cleaned]
    if not usable:
        raise MediaError("no usable audio track in this file")

    def english(a: AudioStream) -> bool:
        return a.language.lower().startswith(prefer_language[:2]) or a.language == ""

    for test in (lambda a: english(a) and a.default,
                 lambda a: a.language.lower().startswith(prefer_language[:2]),
                 lambda a: a.default):
        hit = next((a for a in usable if test(a)), None)
        if hit:
            return hit
    return usable[0]


def extract_for_asr(path: Path, track: AudioStream, dest: Path) -> Path:
    """16 kHz mono WAV, which is what Whisper wants and nothing else does."""
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(), "-i", str(path),
           "-map", f"0:a:{track.audio_index}", "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    out = _run(cmd)
    if out.returncode != 0 or not dest.exists():
        raise MediaError(f"could not extract audio: {out.stderr.strip()[:300]}")
    return dest


def _encoder_for(container: str, channels: int, source_codec: str = "",
                 surround_bitrate: str = "384k",
                 stereo_bitrate: str = "192k") -> tuple[list[str], str]:
    """Codec for the cleaned track, chosen so Plex can direct play it.

    Matching the original's codec is the safest choice available: whatever the
    client already plays for this file, it will play for the clean track too.
    Re-encoding is unavoidable - the audio genuinely changed - so this only
    decides the format and how much the file grows. AC-3 at 640k added 150MB
    to a 200MB episode; E-AC-3 at 384k adds about a third of that and is what
    the file was already using.
    """
    codec = (source_codec or "").lower()
    if channels > 2:
        if codec in ("eac3", "ac3"):
            return (["-c:a", codec, "-b:a", surround_bitrate], codec)
        if container in ("mkv", "mka"):
            return (["-c:a", "eac3", "-b:a", surround_bitrate], "eac3")
        return (["-c:a", "aac", "-b:a", surround_bitrate], "aac")
    return (["-c:a", "aac", "-b:a", stereo_bitrate], "aac")


def render_muted_track(path: Path, track: AudioStream, spans: list[tuple[float, float]],
                       dest: Path, fade: float = 0.02,
                       container: str = "mkv", progress=None,
                       surround_bitrate: str = "384k",
                       stereo_bitrate: str = "192k") -> Path:
    """The source track with `spans` silenced, as a standalone audio file.

    The muting is done with ffmpeg's volume filter rather than by cutting, so
    the track stays exactly as long as the original - a cleaned track that
    drifts out of sync with the picture is worse than no cleaned track.

    A short fade at each edge keeps the mute from clicking. The filter is
    written to a file because a long episode can carry hundreds of spans and
    the expression outgrows a command line.
    """
    args, _codec = _encoder_for(container, track.channels, track.codec,
                                surround_bitrate, stereo_bitrate)
    if spans:
        # volume=0 inside each span, with `fade` seconds of ramp at each edge.
        # Each term is a straight line in time, clamped by min/max.
        terms = []
        for start, end in spans:
            terms.append(
                f"min(1,max(0,(({start:.3f}-t)/{fade:.3f})))"
                f"+min(1,max(0,((t-{end:.3f})/{fade:.3f})))"
            )
        expr = "*".join(f"min(1,({t}))" for t in terms)
        filter_text = f"volume=volume='min(1,max(0,{expr}))':eval=frame"
    else:
        filter_text = "anull"

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf8") as fh:
        fh.write(filter_text)
        filter_file = Path(fh.name)

    try:
        cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(), "-i", str(path),
               "-map", f"0:a:{track.audio_index}", "-vn", "-sn", "-dn",
               "-filter_script:a", str(filter_file), *args, "-progress", "pipe:1",
               "-nostats", str(dest)]
        _run_with_progress(cmd, progress)
    finally:
        filter_file.unlink(missing_ok=True)

    if not dest.exists() or dest.stat().st_size == 0:
        raise MediaError("the cleaned audio track came out empty")
    return dest


def _run_with_progress(cmd: list[str], progress=None) -> None:
    """Run ffmpeg, reporting 0-1 through `progress` as it goes."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    total_us = None
    stderr_tail: list[str] = []
    try:
        for line in proc.stdout or []:
            line = line.strip()
            if line.startswith("out_time_us=") and progress and total_us:
                try:
                    progress(min(1.0, int(line.split("=")[1]) / total_us))
                except (ValueError, ZeroDivisionError):
                    pass
    finally:
        proc.wait()
        if proc.stderr:
            stderr_tail = proc.stderr.read().strip().splitlines()[-5:]
    if proc.returncode != 0:
        raise MediaError("ffmpeg failed: " + " / ".join(stderr_tail)[:400])


def build_cleaned_file(original: Probe, track: AudioStream,
                       spans: list[tuple[float, float]], dest: Path,
                       title: str = CLEAN_TITLE, language: str = "eng",
                       fade: float = 0.02, drop_audio: tuple[int, ...] = (),
                       surround_bitrate: str = "384k", stereo_bitrate: str = "192k",
                       progress=None) -> Path:
    """Silence the words and write the finished file in one pass.

    Doing it in two - render the muted track to its own file, then remux -
    reads the source twice and writes an extra copy of the audio. For a 1GB
    episode that was about a third of the time for no benefit: the filter and
    the encoder settings are identical either way.
    """
    args, _codec = _encoder_for(original.container, track.channels, track.codec,
                                surround_bitrate, stereo_bitrate)
    new_index = len(original.audio) - len(drop_audio)

    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(),
           "-i", str(original.path),
           "-filter_complex_script", str(_filter_script(spans, track, fade)),
           "-map", "0"]
    for index in drop_audio:
        cmd += ["-map", f"-0:a:{index}"]
    cmd += ["-map", "[clean]", "-c", "copy",
            # Only the new stream is encoded; everything else is copied.
            f"-c:a:{new_index}", args[1], f"-b:a:{new_index}", args[3],
            f"-metadata:s:a:{new_index}", f"title={title}",
            # MP4 drops the title and keeps this one. Harmless on Matroska.
            f"-metadata:s:a:{new_index}", f"handler_name={title}",
            f"-metadata:s:a:{new_index}", f"language={language}",
            f"-disposition:a:{new_index}", "0",
            # A muxer option, so it belongs after the inputs - see add_track.
            "-max_interleave_delta", "0",
            "-progress", "pipe:1", "-nostats", str(dest)]
    script = cmd[cmd.index("-filter_complex_script") + 1]
    try:
        _run_with_progress(cmd, progress)
    finally:
        Path(script).unlink(missing_ok=True)
    return dest


def _filter_script(spans: list[tuple[float, float]], track: AudioStream,
                   fade: float) -> Path:
    """The volume curve, written to a file because a long episode carries
    hundreds of spans and the expression outgrows a command line."""
    if spans:
        terms = [
            f"min(1,max(0,(({start:.3f}-t)/{fade:.3f})))"
            f"+min(1,max(0,((t-{end:.3f})/{fade:.3f})))"
            for start, end in spans
        ]
        expr = "*".join(f"min(1,({t}))" for t in terms)
        chain = f"volume=volume='min(1,max(0,{expr}))':eval=frame"
    else:
        chain = "anull"
    text = f"[0:a:{track.audio_index}]{chain}[clean]"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf8") as fh:
        fh.write(text)
        return Path(fh.name)


def add_track(original: Probe, cleaned_audio: Path, dest: Path,
              title: str = CLEAN_TITLE, language: str = "eng",
              drop_audio: tuple[int, ...] = (), progress=None) -> Path:
    """Every stream of the original, copied, plus the cleaned track.

    `drop_audio` holds audio-stream indexes to leave out - used when a file is
    cleaned a second time, so the previous cleaned track is replaced rather
    than joined by a second one.

    The new track is explicitly NOT default: the file must play exactly as it
    did before for anyone who does not choose otherwise.
    """
    new_index = len(original.audio) - len(drop_audio)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(),
           "-i", str(original.path), "-i", str(cleaned_audio), "-map", "0"]
    for index in drop_audio:
        cmd += ["-map", f"-0:a:{index}"]
    cmd += ["-map", "1:a:0", "-c", "copy",
            # Interleave strictly. Left to itself ffmpeg gives up on ordering
            # after max_interleave_delta (10s) when a file has sparse streams -
            # one episode with 38 subtitle tracks ended up with its cleaned
            # audio for the 30-minute mark written 470MB from the matching
            # picture, which plays fine from a local disk and stalls forever
            # over a share.
            #
            # This is a MUXER option, so it belongs here, after the inputs.
            # Written before -i it is read as an input option and silently does
            # nothing - which is exactly what happened on the first attempt,
            # and is why the verify step measures the result rather than
            # trusting the flag.
            "-max_interleave_delta", "0",
            f"-metadata:s:a:{new_index}", f"title={title}",
            # MP4 drops the title and keeps this one. Harmless on Matroska.
            f"-metadata:s:a:{new_index}", f"handler_name={title}",
            f"-metadata:s:a:{new_index}", f"language={language}",
            f"-disposition:a:{new_index}", "0",
            "-progress", "pipe:1", "-nostats", str(dest)]
    _run_with_progress(cmd, progress)
    return dest


def track_bytes(path: Path, audio_index: int) -> int:
    """How much disk one audio track actually occupies.

    Measured by adding its packets up, which takes a few seconds and is right
    in every case. Comparing file sizes before and after is not: re-cleaning a
    file replaces one cleaned track with another, so the difference is zero
    while the track still costs 150MB.
    """
    out = _run([FFPROBE, "-v", "error", "-select_streams", f"a:{audio_index}",
                "-show_entries", "packet=size", "-of", "csv=p=0", str(path)])
    total = 0
    for line in (out.stdout or "").splitlines():
        value = line.rstrip(",").strip()
        if value.isdigit():
            total += int(value)
    return total


def packet_offset(path: Path, stream: str, at: float) -> int | None:
    """Where in the file the first packet of `stream` at time `at` lives."""
    out = _run([FFPROBE, "-v", "error", "-select_streams", stream,
                "-show_entries", "packet=pos", "-of", "csv=p=0",
                "-read_intervals", f"{at}%+#1", str(path)])
    first = (out.stdout or "").strip().splitlines()
    if not first:
        return None
    value = first[0].rstrip(",")
    return int(value) if value.isdigit() else None


# How far the cleaned track may sit from the reference track at the same
# moment. A correctly interleaved file keeps them within a few kilobytes; the
# episode that started this check was 485MB out.
INTERLEAVE_TOLERANCE = 20_000_000


def check_interleave(path: Path, cleaned_index: int, duration: float,
                     reference: int = 0) -> None:
    """Refuse a file whose new track is written far from its own picture.

    This is the one fault that a stream-by-stream inspection misses entirely:
    every track is present, correct and decodes perfectly, and the file still
    stalls forever over a network share because the player has to seek half a
    gigabyte for each second of sound. So it is measured directly, at several
    points, before the file is allowed anywhere near the library.
    """
    if duration <= 0:
        return
    points = [t for t in (duration * 0.1, duration * 0.5, duration * 0.9) if t > 1]
    for at in points:
        here = packet_offset(path, f"a:{cleaned_index}", at)
        there = packet_offset(path, f"a:{reference}", at)
        if here is None or there is None:
            continue
        gap = abs(here - there)
        if gap > INTERLEAVE_TOLERANCE:
            raise MediaError(
                f"the cleaned track is badly interleaved - at {at:.0f}s it sits "
                f"{gap / 1e6:.0f}MB away from the other audio, which stalls "
                f"playback over a share")


def verify_replacement(original: Probe, candidate: Path,
                       expected_audio: int | None = None) -> Probe:
    """Refuse a file that lost anything on the way through."""
    got = probe(candidate)
    expected = len(original.audio) + 1 if expected_audio is None else expected_audio
    if got.video_streams != original.video_streams:
        raise MediaError(
            f"the new file has {got.video_streams} video streams, "
            f"the original had {original.video_streams}")
    if len(got.audio) != expected:
        raise MediaError(
            f"the new file has {len(got.audio)} audio tracks, expected {expected}")
    if got.cleaned_track is None:
        raise MediaError("the new file has no track titled " + CLEAN_TITLE)
    if original.duration and abs(got.duration - original.duration) > DURATION_TOLERANCE:
        raise MediaError(
            f"the new file is {got.duration:.1f}s long, the original was "
            f"{original.duration:.1f}s")
    cleaned = got.cleaned_track
    if cleaned is not None and cleaned.audio_index != 0:
        check_interleave(candidate, cleaned.audio_index, got.duration)
    return got


def remove_cleaned_track(original: Probe, dest: Path, progress=None) -> int:
    """Write the file back without its cleaned track. Returns how many it dropped."""
    drop = [a.audio_index for a in original.audio if a.is_cleaned]
    if not drop:
        return 0
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", *_threads(),
           "-i", str(original.path), "-map", "0"]
    for index in drop:
        cmd += ["-map", f"-0:a:{index}"]
    # After the inputs: -max_interleave_delta is a muxer option (see add_track).
    cmd += ["-c", "copy", "-max_interleave_delta", "0",
            "-progress", "pipe:1", "-nostats", str(dest)]
    _run_with_progress(cmd, progress)
    return len(drop)


def swap_in(new_file: Path, original: Path, keep_backup: bool = False) -> None:
    """Put the new file where the old one was, keeping how it looked to others.

    Owner, permissions and modification time are carried over: Plex treats
    mtime as when the episode arrived, and a library that suddenly thinks
    every cleaned episode is new would reorder itself on every run.
    """
    st = original.stat()
    backup = original.with_suffix(original.suffix + ".cleanarr-backup")
    if keep_backup:
        shutil.copy2(original, backup)
    os.replace(new_file, original)
    try:
        os.chown(original, st.st_uid, st.st_gid)
    except (AttributeError, PermissionError, OSError):
        pass  # Windows, or a share that does not allow it
    try:
        os.chmod(original, st.st_mode)
        os.utime(original, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def free_space(path: Path) -> int:
    return shutil.disk_usage(path.parent if path.is_file() else path).free
