"""What counts as our track, and what name gets written on it.

No ffmpeg needed: the probe is built by hand and the one ffmpeg call is
intercepted, so this checks the naming decisions rather than the muxing.

Run: python tools/test_media.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from cleanarr import media  # noqa: E402

failures: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       got:  {got!r}\n       want: {want!r}")
        failures.append(name)


def track(index: int, title: str = "", handler: str = "", mark: str = "",
          language: str = "eng", default: bool = False) -> media.AudioStream:
    return media.AudioStream(
        index=index, audio_index=index, codec="eac3", channels=6,
        sample_rate=48000, language=language, title=title, default=default,
        handler=handler, mark=mark)


def probe(*audio: media.AudioStream) -> media.Probe:
    return media.Probe(path=Path("episode.mkv"), duration=1200.0,
                       video_streams=1, audio=list(audio), container="mkv")


print("the name comes from the settings")
media.set_clean_title("English - Censored")
check("the configured name is what gets written", media.CLEAN_TITLE,
      "English - Censored")
check("a track with that name is ours",
      track(1, title="English - Censored").is_cleaned, True)
# verify_replacement looks for the track by name: with a renamed track it has
# to find the one just written, not the default name.
check("verify finds the renamed track",
      probe(track(0, title="Surround 5.1", default=True),
            track(1, title="English - Censored")).cleaned_track is not None, True)

print("a rename does not orphan what was already cleaned")
media.set_clean_title("English - Censored", ["Cleaned - English"])
check("the old name is still ours",
      track(1, title="Cleaned - English").is_cleaned, True)
check("so re-cleaning replaces that track",
      [a.audio_index for a in
       probe(track(0, title="Surround 5.1"), track(1, title="Cleaned - English")).audio
       if a.is_cleaned], [1])
check("the default name is always recognised, unconfigured",
      (media.set_clean_title("Something Else"),
       track(1, title="Cleaned - English").is_cleaned)[1], True)

print("the mark outlives any name")
media.set_clean_title("Something Else Entirely")
check("a marked track is ours whatever it is called",
      track(1, title="Nothing Like It", mark="1").is_cleaned, True)
check("an unmarked, unnamed track is not ours", track(1).is_cleaned, False)
check("MP4 keeps the name in handler_name",
      (media.set_clean_title("English - Censored"),
       track(1, handler="English - Censored").is_cleaned)[1], True)
check("an untouched MP4 track is not ours",
      track(1, handler="SoundHandler").is_cleaned, False)

print("picking what to listen to")
media.set_clean_title("Cleaned - English")
p = probe(track(0, title="Surround 5.1", default=True),
          track(1, title="Cleaned - English"))
check("never the cleaned track", media.pick_source_track(p).audio_index, 0)
collide = probe(track(0, title="Cleaned - English", default=True))
try:
    media.pick_source_track(collide)
    got = "no error"
except media.MediaError as exc:
    got = "named" if "pick a different name" in str(exc) else str(exc)
check("a name that collides with the file's own track says so", got, "named")

print("what ffmpeg is told")
calls: list[list[str]] = []
media._run_with_progress = lambda cmd, progress=None: calls.append(cmd)  # noqa: SLF001
media.set_clean_title("English - Censored")
media.build_cleaned_file(
    probe(track(0, title="Surround 5.1", default=True)),
    track(0, title="Surround 5.1", default=True),
    [(10.0, 10.5)], Path("out.mkv"))
cmd = calls[-1]
check("the new track is named as configured",
      "title=English - Censored" in cmd, True)
check("and the MP4 fallback carries the same name",
      "handler_name=English - Censored" in cmd, True)
check("and the mark is written too",
      f"{media.MARK_KEY}={media.MARK_VALUE}" in cmd, True)
check("the new track is the last one", "-metadata:s:a:1" in cmd, True)

print()
if failures:
    print(f"{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("all media checks passed")
