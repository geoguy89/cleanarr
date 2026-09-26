"""The "that was wrong" button: a detection's word onto a list."""

from __future__ import annotations

import pytest

from cleanarr import config, db, words


@pytest.fixture
def detection(settings):
    job = db.enqueue(kind="episode", title="Show", path="/tv/a.mkv")
    db.save_detections(job, [words.Match(1.0, 1.3, "Dick,", "slurs_sexual"),
                             words.Match(2.0, 2.3, "god", "blasphemy", True)],
                       [words.Match(3.0, 3.3, "Prick", "strong", True, "left in")])
    return {r["text"]: r["id"] for r in db.detections(job)}


def test_never_mute(client, detection):
    r = client.post(f"/api/detections/{detection['Dick,']}/correct", json={"list": "never"})
    assert r.status_code == 200
    assert r.json()["word"] == "dick" and r.json()["added"] is True
    assert config.load().allow_words == ["dick"]
    again = client.post(f"/api/detections/{detection['Dick,']}/correct", json={"list": "never"})
    assert again.json()["added"] is False
    assert config.load().allow_words == ["dick"]


def test_never_mute_fixes_a_phrase_word(client, detection):
    client.post(f"/api/detections/{detection['god']}/correct", json={"list": "never"})
    s = config.load()
    m = words.Matcher(never=frozenset(words.NEVER) | set(s.allow_words))
    got = m.find([{"word": w, "start": i, "end": i + 0.3}
                  for i, w in enumerate("oh my god".split())])
    assert got == []


def test_check_in_context(client, settings, detection):
    settings.check_in_context = []
    config.save(settings)
    r = client.post(f"/api/detections/{detection['Dick,']}/correct", json={"list": "context"})
    assert r.json()["judge_configured"] is False       # the UI warns about this
    assert config.load().check_in_context == ["dick"]


def test_always_takes_it_off_the_other_lists(client, settings, detection):
    settings.check_in_context = ["prick", "cock"]
    settings.allow_words = ["Prick"]
    config.save(settings)
    client.post(f"/api/detections/{detection['Prick']}/correct", json={"list": "always"})
    s = config.load()
    assert s.check_in_context == ["cock"]
    assert s.allow_words == []
    assert s.custom_words == []       # already on the built-in list


def test_always_adds_a_word_no_list_has(client, settings):
    job = db.enqueue(kind="episode", title="Show", path="/tv/b.mkv")
    db.save_detections(job, [], [words.Match(1.0, 1.2, "muppet", "custom")])
    det = db.detections(job)[0]["id"]
    client.post(f"/api/detections/{det}/correct", json={"list": "always"})
    assert config.load().custom_words == ["muppet"]


def test_undo(client, detection):
    client.post(f"/api/detections/{detection['Dick,']}/correct", json={"list": "never"})
    assert client.delete("/api/words/never/Dick").json() == {"removed": 1}
    assert config.load().allow_words == []
    assert client.delete("/api/words/bogus/x").status_code == 404


def test_bad_requests(client, detection):
    assert client.post("/api/detections/999/correct", json={"list": "never"}).status_code == 404
    assert client.post(f"/api/detections/{detection['god']}/correct",
                       json={"list": "sometimes"}).status_code == 400
