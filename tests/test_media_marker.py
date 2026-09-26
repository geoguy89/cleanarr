"""What counts as our track, and what gets written on it. No ffmpeg needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from cleanarr import media


@pytest.fixture(autouse=True)
def _title():
    media.set_clean_title(media.DEFAULT_CLEAN_TITLE)
    yield
    media.set_clean_title(media.DEFAULT_CLEAN_TITLE)


def track(index: int, title: str = "", handler: str = "", mark: str = "",
          language: str = "eng", default: bool = False, channels: int = 6,
          codec: str = "eac3") -> media.AudioStream:
    return media.AudioStream(
        index=index, audio_index=index, codec=codec, channels=channels,
        sample_rate=48000, language=language, title=title, default=default,
        handler=handler, mark=mark)


def probe(*audio: media.AudioStream, container: str = "mkv") -> media.Probe:
    return media.Probe(path=Path(f"episode.{container}"), duration=1200.0,
                       video_streams=1, audio=list(audio), container=container)


def test_the_name_comes_from_the_settings():
    media.set_clean_title("English - Censored")
    assert media.CLEAN_TITLE == "English - Censored"
    assert track(1, title="English - Censored").is_cleaned
    assert probe(track(0, title="Surround 5.1", default=True),
                 track(1, title="English - Censored")).cleaned_track is not None


def test_blank_name_falls_back_to_the_default():
    media.set_clean_title("   ")
    assert media.CLEAN_TITLE == media.DEFAULT_CLEAN_TITLE


def test_a_rename_does_not_orphan_what_was_already_cleaned():
    media.set_clean_title("English - Censored", ["Old Name"])
    assert track(1, title="Old Name").is_cleaned
    assert [a.audio_index for a in probe(track(0, title="Surround 5.1"),
                                         track(1, title="Old Name")).audio
            if a.is_cleaned] == [1]
    media.set_clean_title("Something Else")
    assert track(1, title="Cleaned - English").is_cleaned     # default always known
    assert not track(1, title="Old Name").is_cleaned           # not configured now


def test_the_mark_outlives_any_name():
    media.set_clean_title("Something Else Entirely")
    assert track(1, title="Nothing Like It", mark="1").is_cleaned
    assert not track(1).is_cleaned


def test_mp4_keeps_only_handler_name():
    media.set_clean_title("English - Censored")
    assert track(1, handler="English - Censored").is_cleaned
    assert not track(1, handler="SoundHandler").is_cleaned
    assert track(1, handler="english - censored").is_cleaned   # case-insensitive


def test_written_here_needs_proof_not_a_name():
    media.set_clean_title("English - Censored")
    assert track(1, title="Anything", mark="1").written_here
    assert track(1, title="English - Censored", handler="English - Censored").written_here
    lookalike = track(1, title="English - Censored", handler="SoundHandler")
    assert not lookalike.written_here
    assert lookalike.is_cleaned          # still never used as a source
    assert not track(0, title="Surround 5.1", handler="SoundHandler").written_here


def test_pick_source_never_takes_the_cleaned_track():
    p = probe(track(0, title="Surround 5.1", default=True), track(1, title="Cleaned - English"))
    assert media.pick_source_track(p).audio_index == 0


def test_pick_source_preference_order():
    # default English first
    p = probe(track(0, language="fre", default=False), track(1, language="eng", default=True))
    assert media.pick_source_track(p).audio_index == 1
    # then any English
    p = probe(track(0, language="fre", default=True), track(1, language="eng"))
    assert media.pick_source_track(p).audio_index == 1
    # then the default
    p = probe(track(0, language="fre"), track(1, language="ger", default=True))
    assert media.pick_source_track(p).audio_index == 1
    # then the first
    p = probe(track(0, language="fre"), track(1, language="ger"))
    assert media.pick_source_track(p).audio_index == 0


def test_a_name_that_collides_with_the_files_own_track_says_so():
    with pytest.raises(media.MediaError, match="pick a different name"):
        media.pick_source_track(probe(track(0, title="Cleaned - English", default=True)))
    with pytest.raises(media.MediaError, match="no usable audio"):
        media.pick_source_track(probe())


@pytest.mark.parametrize("container, channels, codec, want", [
    ("mkv", 6, "eac3", "eac3"),
    ("mkv", 6, "ac3", "ac3"),
    ("mkv", 6, "dts", "eac3"),
    ("mp4", 6, "dts", "aac"),
    ("mp4", 6, "eac3", "eac3"),
    ("mkv", 2, "eac3", "aac"),
    ("mp4", 1, "aac", "aac"),
])
def test_encoder_choice(container, channels, codec, want):
    args, chosen = media._encoder_for(container, channels, codec, "384k", "192k")
    assert chosen == want
    assert args[1] == want
    assert args[3] == ("384k" if channels > 2 else "192k")


# ---------------------------------------------------------------- the command

def _capture(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(media, "_run_with_progress",
                        lambda cmd, progress=None: calls.append(cmd))
    return calls


def _after_inputs(cmd: list[str], flag: str) -> bool:
    last_input = max(i for i, c in enumerate(cmd) if c == "-i")
    return any(c == flag for c in cmd[last_input:])


def test_build_command_names_and_marks_the_new_track(monkeypatch):
    calls = _capture(monkeypatch)
    media.set_clean_title("English - Censored")
    src = track(0, title="Surround 5.1", default=True)
    media.build_cleaned_file(probe(src), src, [(10.0, 10.5)], Path("out.mkv"))
    cmd = calls[-1]
    assert "title=English - Censored" in cmd
    assert "handler_name=English - Censored" in cmd
    assert f"{media.MARK_KEY}={media.MARK_VALUE}" in cmd
    assert "-metadata:s:a:1" in cmd
    assert cmd[cmd.index("-disposition:a:1") + 1] == "0"      # never the default


def test_build_command_replaces_a_previous_track(monkeypatch):
    calls = _capture(monkeypatch)
    src = track(0, title="Surround 5.1", default=True)
    old = track(1, title="Cleaned - English", mark="1")
    media.build_cleaned_file(probe(src, old), src, [(1, 2)], Path("out.mkv"),
                             drop_audio=(1,))
    cmd = calls[-1]
    assert "-0:a:1" in cmd
    assert "-metadata:s:a:1" in cmd and "-metadata:s:a:2" not in cmd


def test_every_remux_interleaves_strictly(monkeypatch):
    calls = _capture(monkeypatch)
    src = track(0, title="Surround 5.1", default=True)
    ours = track(1, title="Cleaned - English", mark="1")
    media.build_cleaned_file(probe(src), src, [(1, 2)], Path("a.mkv"))
    media.add_track(probe(src), Path("clean.mka"), Path("b.mkv"))
    media.remove_cleaned_track(probe(src, ours), Path("c.mkv"))
    assert len(calls) == 3
    for cmd in calls:
        # A muxer option: only honoured after the inputs.
        assert _after_inputs(cmd, "-max_interleave_delta")
        assert cmd[cmd.index("-max_interleave_delta") + 1] == "0"


def test_remove_does_nothing_without_a_track_to_drop(monkeypatch):
    calls = _capture(monkeypatch)
    assert media.remove_cleaned_track(probe(track(0)), Path("x.mkv")) == 0
    assert calls == []


def test_filter_script_mutes_each_span(tmp_path):
    src = track(0)
    script = media._filter_script([(1.0, 2.0), (5.0, 5.5)], src, 0.02)
    try:
        text = script.read_text()
    finally:
        script.unlink()
    assert text.startswith("[0:a:0]volume=")
    assert text.endswith("[clean]")
    assert "1.000" in text and "2.000" in text and "5.500" in text
    empty = media._filter_script([], src, 0.02)
    try:
        assert empty.read_text() == "[0:a:0]anull[clean]"
    finally:
        empty.unlink()
