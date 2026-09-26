"""One file start to finish, with real ffmpeg and a fixed transcript."""

from __future__ import annotations

import json

import pytest

from cleanarr import asr, config, db, judge, media, pipeline
from mediakit import make_media, rms, samples

pytestmark = pytest.mark.ffmpeg

WORDS = [
    {"word": " what", "start": 0.5, "end": 0.7},
    {"word": " the", "start": 0.7, "end": 0.9},
    {"word": " fuck,", "start": 1.0, "end": 1.4},
    {"word": " some", "start": 2.0, "end": 2.2},
    {"word": " cock", "start": 3.0, "end": 3.4},
    {"word": " please", "start": 4.0, "end": 4.4},
]


@pytest.fixture
def fake_asr(monkeypatch):
    calls = []

    def transcribe(audio, **kw):
        calls.append(kw)
        return asr.Transcript(words=list(WORDS), language="en", duration=6.0,
                              model="fake")
    monkeypatch.setattr(asr, "transcribe", transcribe)
    return calls


@pytest.fixture(params=["mkv", "mp4"])
def episode(request, tmp_path):
    folder = tmp_path / "tv"
    folder.mkdir()
    return make_media(folder / f"e1.{request.param}", seconds=6.0)


def run(path, settings, cache, **kw) -> dict:
    job = db.enqueue(kind="episode", title="Show", path=str(path), **kw)
    pipeline.run_job(job, settings, cache)
    return dict(db.get(job))


def test_cleans_marks_and_records(home, settings, fake_asr, episode):
    job = run(episode, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 2
    assert job["added_bytes"] > 0
    p = media.probe(episode)
    assert p.cleaned_track is not None and p.cleaned_track.written_here
    pcm, loud = samples(episode, 1), rms(samples(episode, 0), 0.1, 0.4)
    assert rms(pcm, 1.0, 1.4) < loud * 0.01          # "fuck"
    assert rms(pcm, 3.0, 3.4) < loud * 0.01          # "cock", no judge configured
    assert rms(pcm, 2.0, 2.6) > loud * 0.5
    rows = db.detections(job["id"])
    assert [(r["text"], r["muted"]) for r in rows] == [("fuck,", 1), ("cock", 1)]
    assert not list(episode.parent.glob("cleanarr-*"))     # scratch removed


def test_a_cleaned_file_is_skipped_unless_forced(home, settings, fake_asr, episode):
    run(episode, settings, home / "cache")
    again = run(episode, settings, home / "cache")
    assert again["status"] == "skipped"
    forced = run(episode, settings, home / "cache", force=True)
    assert forced["status"] == "done", forced["message"]
    assert len(media.probe(episode).audio) == 2              # replaced, not added


def test_remove_takes_the_track_back_out(home, settings, fake_asr, episode):
    run(episode, settings, home / "cache")
    removed = run(episode, settings, home / "cache", action="remove")
    assert removed["status"] == "done", removed["message"]
    assert media.probe(episode).cleaned_track is None
    assert not db.cleaned_before(str(episode))
    nothing = run(episode, settings, home / "cache", action="remove")
    assert nothing["status"] == "skipped"


def test_a_lookalike_track_is_never_replaced(home, settings, fake_asr, tmp_path):
    src = make_media(tmp_path / "e.mkv", seconds=3.0, title="English")
    settings.track_title = "English"
    job = run(src, settings, home / "cache")
    assert job["status"] == "failed"
    assert "nothing in the file says" in job["message"] or "pick a different name" in job["message"]
    assert len(media.probe(src).audio) == 1


def test_nothing_to_mute_is_skipped(home, settings, monkeypatch, episode):
    monkeypatch.setattr(asr, "transcribe", lambda audio, **kw: asr.Transcript(
        words=[{"word": "hello", "start": 1, "end": 1.2}], language="en",
        duration=6.0, model="fake"))
    job = run(episode, settings, home / "cache")
    assert job["status"] == "skipped"
    assert "nothing to mute" in job["message"]
    assert media.probe(episode).cleaned_track is None


def test_the_judge_can_leave_a_word_in(home, settings, fake_asr, stub, episode):
    judge._style.clear()

    def generate(req):
        body = req.json()
        if body.get("prompt") == "":
            return 200, {}
        return 200, {"response": json.dumps([{"n": 1, "verdict": "CLEAN", "instead": "caulk"}])}
    stub.route("POST", "/api/generate", generate)
    stub.route("GET", "/api/ps", {"models": []})
    settings.judge_url = stub.url
    job = run(episode, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 1
    rows = {r["text"]: r for r in db.detections(job["id"])}
    assert rows["cock"]["muted"] == 0
    assert "caulk" in rows["cock"]["reason"]
    pcm, loud = samples(episode, 1), rms(samples(episode, 0), 0.1, 0.4)
    assert rms(pcm, 3.0, 3.4) > loud * 0.5          # left audible


def test_an_unanswered_judge_leaves_the_word_muted(home, settings, fake_asr, episode):
    settings.judge_url = "http://127.0.0.1:9"
    job = run(episode, settings, home / "cache")
    assert job["muted"] == 2


def test_allow_and_custom_words_apply(home, settings, fake_asr, episode):
    settings.allow_words = ["fuck"]
    settings.custom_words = ["please"]
    job = run(episode, settings, home / "cache")
    texts = sorted(r["text"] for r in db.detections(job["id"]))
    assert texts == ["cock", "please"]


def test_cancel_stops_before_anything_is_written(home, settings, fake_asr, episode):
    job = db.enqueue(kind="episode", title="Show", path=str(episode))
    pipeline.run_job(job, settings, home / "cache", should_cancel=lambda: True)
    assert db.get(job)["status"] == "cancelled"
    assert media.probe(episode).cleaned_track is None


def test_missing_file_explains_itself(home, settings, tmp_path):
    job = run(tmp_path / "gone" / "e.mkv", settings, home / "cache")
    assert job["status"] == "failed"
    assert "not found from this container" in job["message"]


def test_settings_reach_the_listener(home, settings, fake_asr, episode):
    settings.asr_backend = "remote"
    settings.asr_url = "http://whisper"
    settings.asr_remote_model = "m"
    settings.trim_silence = True
    run(episode, settings, home / "cache")
    kw = fake_asr[-1]
    assert kw["remote_url"] == "http://whisper" and kw["remote_model"] == "m"
    assert kw["trim_silence"] is True
    settings.asr_backend = "builtin"
    run(episode, settings, home / "cache", force=True)
    assert fake_asr[-1]["remote_url"] == ""
