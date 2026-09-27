"""Speech in, cleaned track out: espeak-ng, real Whisper (tiny.en on CPU), real ffmpeg.

Slow the first time (downloads tiny.en, ~75 MB). Set CLEANARR_TEST_MODELS to a
folder to keep the model between runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cleanarr import config, db, media, pipeline, words
from mediakit import audio_tags, make_media, rms, samples, srt, streams

pytestmark = [pytest.mark.e2e, pytest.mark.ffmpeg]

SPEECH = ("Well, what the fuck is that? I told you, this is bullshit. "
          "Now pass the salt, please. Oh shit, I forgot the bread.")
EXPECTED = {"fuck", "bullshit", "shit"}
# Subtitles for SPEECH, roughly timed as a subtitler would.
CUES = [(0.0, 2.4, "Well, what the fuck is that?"), (2.5, 5.2, "I told you, this is bullshit."),
        (5.2, 7.3, "Now pass the salt, please."), (7.4, 9.8, "Oh shit, I forgot the bread.")]


@pytest.fixture(scope="module")
def speech(tmp_path_factory) -> Path:
    if not shutil.which("espeak-ng"):
        pytest.skip("espeak-ng not installed")
    pytest.importorskip("faster_whisper")
    wav = tmp_path_factory.mktemp("speech") / "speech.wav"
    subprocess.run(["espeak-ng", "-s", "140", "-w", str(wav), SPEECH], check=True)
    return wav


@pytest.fixture(scope="module")
def models(tmp_path_factory) -> Path:
    keep = os.environ.get("CLEANARR_TEST_MODELS")
    folder = Path(keep) if keep else tmp_path_factory.mktemp("models")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.mark.parametrize("container", ["mkv", "mp4"])
def test_speech_is_cleaned_end_to_end(home, settings, speech, models, container, tmp_path):
    folder = tmp_path / "tv" / "Show"
    folder.mkdir(parents=True)
    episode = make_media(folder / f"S01E01.{container}", seconds=11.0, audio=speech, cues=CUES)
    before = media.probe(episode)
    original_pcm = samples(episode, 0)

    cache = home / "cache"
    cache.mkdir()
    (cache / "models").symlink_to(models, target_is_directory=True)
    settings.model, settings.device, settings.compute_type = "tiny.en", "cpu", "int8"
    config.save(settings)

    job = db.enqueue(kind="episode", title="Show", subtitle="S01E01", path=str(episode))
    pipeline.run_job(job, config.load(), cache)
    row = db.get(job)
    assert row["status"] == "done", row["message"]

    found = {words.normalize(d["text"]) for d in db.detections(job)}
    assert EXPECTED <= found, f"heard {found}"
    assert row["muted"] == len(db.detections(job))
    # The guardrails recorded their evidence: Whisper's confidence for each
    # word, and subtitles that agree with what was heard.
    for d in db.detections(job):
        assert d["confidence"] is not None and 0 < d["confidence"] <= 1
        assert d["subtitle_state"] == "agrees", (d["text"], d["subtitle"])

    # Marked: every original stream is still there plus the new track.
    after = media.probe(episode)
    assert len(after.audio) == 2
    assert [s["codec_type"] for s in streams(episode)].count("subtitle") == 1
    ours = after.cleaned_track
    assert ours is not None and ours.audio_index == 1 and ours.written_here
    tags = audio_tags(episode, 1)
    assert tags["handler_name"] == media.CLEAN_TITLE
    if container == "mkv":
        assert tags["title"] == media.CLEAN_TITLE
        assert tags[media.MARK_KEY] == media.MARK_VALUE
    assert after.audio[0].default and not after.audio[1].default

    # Verified: the same check the pipeline ran, run again on the result.
    media.verify_replacement(before, episode)

    # Muted inside each span, and speech still there outside them.
    spans = words.to_spans(
        [words.Match(d["start"], d["end"], d["text"], d["category"])
         for d in db.detections(job)], settings.pad_start, settings.pad_end)
    clean = samples(episode, 1)
    speech_level = rms(original_pcm, 0.0, 10.0)
    for start, end in spans:
        inner = (start + settings.fade + 0.01, end - settings.fade - 0.01)
        assert rms(original_pcm, *inner) > speech_level * 0.2, "span was not over speech"
        assert rms(clean, *inner) < speech_level * 0.01, f"audible inside {start}-{end}"
    gaps = [(a[1] + 0.05, b[0] - 0.05) for a, b in zip(spans, spans[1:]) if b[0] - a[1] > 0.6]
    assert gaps
    for start, end in gaps:
        assert rms(clean, start, end) == pytest.approx(rms(original_pcm, start, end), rel=0.2)


# ---------------------------------------------------------------- timing

def test_a_late_audio_track_is_muted_where_the_words_are(home, settings, speech, models, tmp_path):
    """TV recordings often start the audio after the picture. Whisper counts
    from the first sample; the mutes and the subtitles count from the start
    of the file."""
    folder = tmp_path / "tv"
    folder.mkdir()
    delay = 2.0
    episode = make_media(folder / "S01E01.mkv", seconds=13.0, audio=speech, audio_delay=delay,
                         cues=[(a + delay, b + delay, t) for a, b, t in CUES])
    original = samples(episode, 0, file_clock=True)
    job = _clean(home, settings, models, episode, subtitle_opinion=True)
    row = db.get(job)
    assert row["status"] == "done", row["message"]
    found = db.detections(job)
    assert EXPECTED <= {words.normalize(d["text"]) for d in found}
    for d in found:
        assert d["subtitle_state"] == "agrees", (d["text"], d["start"], d["subtitle"])
    clean = samples(episode, 1, file_clock=True)
    level = rms(original, 0.0, 12.0)
    for d in found:
        inner = (d["start"] + 0.04, d["end"] - 0.04)
        assert rms(original, *inner) > level * 0.2, f"{d['text']} is not where the speech is"
        assert rms(clean, *inner) < level * 0.01, f"{d['text']} still audible"


SENTENCES = [
    "Good morning everyone, thanks for coming in so early today.",
    "Well, what the fuck is that thing on the table?",
    "I told you already, this whole plan is bullshit.",
    "Now please pass the salt and the pepper to your brother.",
    "The weather report says it will rain again this afternoon.",
    "Oh shit, I completely forgot to bring the bread.",
    "We need to finish painting the fence before the weekend.",
    "My grandmother makes the best apple pie in the county.",
    "Get your ass over here and help me carry these boxes.",
    "The train leaves the station at a quarter past seven.",
    "Holy shit, look at the size of that enormous dog.",
    "Remember to water the tomatoes before you go to bed.",
]


@pytest.fixture(scope="module")
def dialogue(tmp_path_factory):
    """A minute of speech, sentence by sentence, and exactly when each was said."""
    if not shutil.which("espeak-ng"):
        pytest.skip("espeak-ng not installed")
    pytest.importorskip("faster_whisper")
    import wave
    folder = tmp_path_factory.mktemp("dialogue")
    parts, cues, t = [], [], 0.5
    for i, text in enumerate(SENTENCES):
        part = folder / f"{i}.wav"
        subprocess.run(["espeak-ng", "-s", "150", "-w", str(part), text], check=True)
        with wave.open(str(part)) as w:
            length = w.getnframes() / w.getframerate()
        cues.append((t, t + length, text))
        parts.append(part)
        t += length + 0.6
    inputs = [x for part in parts for x in ("-i", str(part))]
    chain = (";".join(f"[{i}:a]apad=pad_dur=0.6[p{i}]" for i in range(len(parts))) + ";"
             + "".join(f"[p{i}]" for i in range(len(parts)))
             + f"concat=n={len(parts)}:v=0:a=1,adelay=500:all=1[a]")
    wav = folder / "dialogue.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex", chain,
                    "-map", "[a]", "-ar", "48000", "-ac", "2", str(wav)], check=True)
    return wav, cues


def test_a_subtitle_file_for_another_release_is_lined_up(home, settings, models, dialogue, tmp_path):
    """A .srt beside the file, 8.5 s late, on a file whose audio starts 1.5 s in."""
    wav, cues = dialogue
    delay, late = 1.5, 8.5
    folder = tmp_path / "tv"
    folder.mkdir()
    episode = make_media(folder / "S01E01.mkv", seconds=cues[-1][1] + delay + 1.5,
                         audio=wav, audio_delay=delay, subtitles=False)
    srt(folder / "S01E01.en.srt", 0, [(a + delay + late, b + delay + late, t) for a, b, t in cues])
    job = _clean(home, settings, models, episode, subtitle_opinion=True)
    row = db.get(job)
    assert row["status"] == "done", row["message"]
    assert "subtitles moved" in row["message"]
    found = db.detections(job)
    assert {"fuck", "bullshit", "shit", "ass"} <= {words.normalize(d["text"]) for d in found}
    for d in found:
        assert d["subtitle_state"] == "agrees", (d["text"], d["start"], d["subtitle"])
        # In the very line it was said in, on the file's clock.
        said = [c for a, b, c in cues if a + delay - 0.3 <= d["start"] <= b + delay + 0.3]
        assert said and d["subtitle"].startswith(said[0][:20]), (d["text"], d["subtitle"])


def _clean(home, settings, models, episode, **changes) -> int:
    cache = home / "cache"
    cache.mkdir(exist_ok=True)
    if not (cache / "models").exists():
        (cache / "models").symlink_to(models, target_is_directory=True)
    settings.model, settings.device, settings.compute_type = "tiny.en", "cpu", "int8"
    for key, value in changes.items():
        setattr(settings, key, value)
    config.save(settings)
    job = db.enqueue(kind="episode", title="Show", subtitle="S01E01", path=str(episode))
    pipeline.run_job(job, config.load(), cache)
    return job
