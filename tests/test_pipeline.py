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


def test_every_muting_setting_reaches_ffmpeg(home, settings, fake_asr, episode, monkeypatch):
    seen = {}
    real = media.build_cleaned_file

    def spy(original, track, spans, dest, **kw):
        seen.update(kw, spans=spans)
        return real(original, track, spans, dest, **kw)
    monkeypatch.setattr(media, "build_cleaned_file", spy)
    settings.pad_start, settings.pad_end, settings.fade = 0.3, 0.25, 0.05
    settings.bitrate_stereo, settings.bitrate_surround = "96k", "256k"
    settings.track_title = "Family"
    settings.ffmpeg_threads = 1
    job = run(episode, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert seen["spans"][0] == (0.7, 1.65)            # "fuck" 1.0-1.4, padded
    assert seen["fade"] == 0.05
    assert (seen["stereo_bitrate"], seen["surround_bitrate"]) == ("96k", "256k")
    assert seen["title"] == "Family"
    assert media.THREADS == 1
    pcm, loud = samples(episode, 1), rms(samples(episode, 0), 0.1, 0.4)
    assert rms(pcm, 0.75, 0.95) < loud * 0.01          # the padding is muted too


def test_keep_backup_leaves_the_original_beside_it(home, settings, fake_asr, episode):
    settings.keep_backup = True
    run(episode, settings, home / "cache")
    backup = episode.with_suffix(episode.suffix + ".cleanarr-backup")
    assert backup.exists()
    assert media.probe(backup).cleaned_track is None


def test_listening_settings_reach_the_model(home, settings, fake_asr, episode):
    settings.device, settings.compute_type, settings.model = "cpu", "int8", "small.en"
    settings.asr_backend, settings.asr_url, settings.asr_api_key = "remote", "http://w", "sk"
    run(episode, settings, home / "cache")
    kw = fake_asr[-1]
    assert (kw["device"], kw["compute_type"], kw["model_name"]) == ("cpu", "int8", "small.en")
    assert kw["remote_key"] == "sk"


def test_scratch_folders_left_by_a_killed_run_are_swept(tmp_path):
    import os
    import time
    old = tmp_path / "cleanarr-old"
    fresh = tmp_path / "cleanarr-fresh"
    mine = tmp_path / "Season 01"
    for folder in (old, fresh, mine):
        folder.mkdir()
    stale = time.time() - 7 * 3600
    os.utime(old, (stale, stale))
    pipeline._sweep_stale_work(tmp_path)
    assert not old.exists()
    assert fresh.exists() and mine.exists()


def test_not_enough_space_stops_before_anything_is_written(home, settings, fake_asr,
                                                             episode, monkeypatch):
    monkeypatch.setattr(media, "free_space", lambda path: 1024)
    job = run(episode, settings, home / "cache")
    assert job["status"] == "failed" and "not enough free space" in job["message"]
    assert not list(episode.parent.glob("cleanarr-*"))
    assert media.probe(episode).cleaned_track is None
    assert fake_asr == []                       # it did not even listen


# ---------------------------------------------------------------- guardrails

CUES = [(0.8, 1.6, "What the fuck?"), (2.8, 3.8, "Get some caulk on it."), (4.0, 4.6, "Please.")]


@pytest.fixture(params=["mkv", "mp4"])
def subtitled(request, tmp_path):
    folder = tmp_path / "tv"
    folder.mkdir()
    return make_media(folder / f"e1.{request.param}", seconds=6.0, cues=CUES)


def test_subtitles_and_confidence_are_recorded_but_never_unmute(home, settings, monkeypatch, subtitled):
    heard = [dict(w) for w in WORDS]
    heard[2]["probability"] = 0.97          # "fuck," - sure
    heard[4]["probability"] = 0.31          # "cock" - unsure
    monkeypatch.setattr(asr, "transcribe", lambda audio, **kw: asr.Transcript(
        words=heard, language="en", duration=6.0, model="fake"))
    job = run(subtitled, settings, home / "cache")
    assert job["status"] == "done", job["message"]
    assert job["muted"] == 2                 # both still muted
    rows = {r["text"]: dict(r) for r in db.detections(job["id"])}
    assert rows["fuck,"]["subtitle_state"] == "agrees" and rows["fuck,"]["confidence"] == 0.97
    assert rows["cock"]["subtitle_state"] == "differs"
    assert "caulk" in rows["cock"]["subtitle"]
    assert rows["cock"]["confidence"] == 0.31
    pcm, loud = samples(subtitled, 1), rms(samples(subtitled, 0), 0.1, 0.4)
    assert rms(pcm, 3.0, 3.4) < loud * 0.01
    history = {r["id"]: r["to_check"] for r in db.history()}
    assert history[job["id"]] == 1


def test_a_file_without_subtitles_still_cleans(home, settings, fake_asr, tmp_path):
    src = make_media(tmp_path / "plain.mkv", seconds=6.0, subtitles=False)
    job = run(src, settings, home / "cache")
    assert job["status"] == "done"
    assert {r["subtitle_state"] for r in db.detections(job["id"])} == {""}


def test_a_per_show_exception_applies_to_that_show_only(home, settings, fake_asr, tmp_path):
    settings.allow_words_by_title = {"Show": ["cock"]}
    a = make_media(tmp_path / "a.mkv", seconds=6.0)
    b = make_media(tmp_path / "b.mkv", seconds=6.0)
    here = run(a, settings, home / "cache")                      # titled "Show"
    job = db.enqueue(kind="episode", title="Another Show", path=str(b))
    pipeline.run_job(job, settings, home / "cache")
    assert [r["text"] for r in db.detections(here["id"])] == ["fuck,"]
    assert [r["text"] for r in db.detections(job)] == ["fuck,", "cock"]
