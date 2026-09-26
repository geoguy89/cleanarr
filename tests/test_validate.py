"""Settings are checked when saved, not when a job trips over them."""

from __future__ import annotations

import pytest

from cleanarr import config, validate


@pytest.mark.parametrize("value, want, problem", [
    ("", "", ""),
    ("  http://sonarr:8989/  ", "http://sonarr:8989", ""),
    ("https://example.test/sonarr", "https://example.test/sonarr", ""),
    ("sonarr:8989", "sonarr:8989", "must start with http"),
    ("ftp://x", "ftp://x", "must start with http"),
    ("http://", "http:", "no host name"),
    ("http://host:port", "http://host:port", "port that is not a number"),
])
def test_urls(value, want, problem):
    got, err = validate.url(value, "Sonarr address")
    assert got == want
    assert problem in err if problem else err == ""


def test_a_good_payload_is_cleaned():
    clean, errors = validate.check({
        "sonarr": {"url": "http://s:8989/", "api_key": "k"},
        "pad_start": "0.2", "judge_threads": "4", "bitrate_stereo": "192K",
        "custom_words": "muppet\n  \nmuppet\nnumpty", "keep_backup": "true",
        "categories": ["strong", "mild"], "track_title": "  Family  ",
        "judge_keep_alive": "5m",
    })
    assert errors == {}
    assert clean["sonarr"]["url"] == "http://s:8989"
    assert clean["pad_start"] == 0.2 and clean["judge_threads"] == 4
    assert clean["bitrate_stereo"] == "192k"
    assert clean["custom_words"] == ["muppet", "numpty"]
    assert clean["keep_backup"] is True
    assert clean["track_title"] == "Family"


@pytest.mark.parametrize("payload, field", [
    ({"library_source": "kodi"}, "library_source"),
    ({"device": "rocm"}, "device"),
    ({"hold_policy": "sometimes"}, "hold_policy"),
    ({"pad_start": "abc"}, "pad_start"),
    ({"pad_end": 5}, "pad_end"),
    ({"fade": -1}, "fade"),
    ({"judge_threads": "two"}, "judge_threads"),
    ({"ffmpeg_threads": -1}, "ffmpeg_threads"),
    ({"bitrate_surround": "loud"}, "bitrate_surround"),
    ({"judge_keep_alive": "a while"}, "judge_keep_alive"),
    ({"categories": ["strong", "rude"]}, "categories"),
    ({"custom_words": 5}, "custom_words"),
    ({"track_title": "x" * 81}, "track_title"),
    ({"plex_url": "plex.local:32400"}, "plex_url"),
    ({"radarr": {"url": "radarr"}}, "radarr.url"),
])
def test_bad_values_are_named(payload, field):
    _, errors = validate.check(payload)
    assert field in errors


def test_api_refuses_bad_settings_and_saves_nothing(client, settings):
    r = client.put("/api/settings", json={"pad_start": "abc", "plex_url": "nope",
                                          "custom_words": ["kept?"]})
    assert r.status_code == 400
    body = r.json()
    assert set(body["errors"]) == {"pad_start", "plex_url"}
    assert body["detail"]
    assert config.load().custom_words == []


def test_remote_whisper_needs_an_address(client, settings):
    r = client.put("/api/settings", json={"asr_backend": "remote", "asr_url": ""})
    assert r.status_code == 400 and "asr_url" in r.json()["errors"]
    ok = client.put("/api/settings", json={"asr_backend": "remote",
                                           "asr_url": "http://whisper:8000"})
    assert ok.status_code == 200
    assert config.load().asr_backend == "remote"


def test_judge_needs_a_model(client, settings):
    r = client.put("/api/settings", json={"judge_url": "http://ollama:11434",
                                          "judge_model": ""})
    assert r.status_code == 400 and "judge_model" in r.json()["errors"]


def test_advanced_settings_save(client, settings):
    r = client.put("/api/settings", json={
        "compute_type": "int8", "bitrate_surround": "448k", "bitrate_stereo": "160k",
        "ffmpeg_threads": 4, "judge_keep_alive": "0"})
    assert r.status_code == 200
    s = config.load()
    assert (s.compute_type, s.bitrate_surround, s.bitrate_stereo, s.ffmpeg_threads,
            s.judge_keep_alive) == ("int8", "448k", "160k", 4, "0")
