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
from mediakit import audio_tags, make_media, rms, samples, streams

pytestmark = [pytest.mark.e2e, pytest.mark.ffmpeg]

SPEECH = ("Well, what the fuck is that? I told you, this is bullshit. "
          "Now pass the salt, please. Oh shit, I forgot the bread.")
EXPECTED = {"fuck", "bullshit", "shit"}


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
    episode = make_media(folder / f"S01E01.{container}", seconds=11.0, audio=speech)
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
