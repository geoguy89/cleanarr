"""When the queue waits for the media server, and what it says while waiting."""

from __future__ import annotations

import pytest

from cleanarr import arr, config


def session(video="directplay", audio="copy", who="kid", what="Bluey"):
    return arr.PlexSession(who=who, what=what, state="playing",
                           video_decision=video, audio_decision=audio, hardware=True)


DIRECT = session()
AUDIO = session(audio="transcode")
VIDEO = session(video="transcode", who="dad", what="Film")


@pytest.mark.parametrize("policy, sessions, hold", [
    ("never", [VIDEO], False),
    ("video_transcode", [DIRECT, AUDIO], False),
    ("video_transcode", [DIRECT, VIDEO], True),
    ("any_transcode", [DIRECT], False),
    ("any_transcode", [AUDIO], True),
    ("playing", [DIRECT], True),
    ("playing", [], False),
    ("something-unknown", [VIDEO], True),   # unknown falls back to the default
])
def test_should_hold(policy, sessions, hold):
    assert arr.should_hold(sessions, policy)[0] is hold


def test_the_reason_names_who_and_how():
    hold, why = arr.should_hold([VIDEO, session(video="transcode")], "video_transcode")
    assert hold
    assert why == "dad is watching Film (transcoding video on the GPU) (+1 more)"
    assert "direct playing" in DIRECT.describe()
    assert "audio only" in AUDIO.describe()


def test_policy_labels_name_the_server():
    labels = {p["key"]: p["label"] for p in arr.hold_policies("jellyfin")}
    assert "Jellyfin" in labels["video_transcode"]
    assert "your media server" in arr.hold_policies("none")[1]["label"]
    assert list(labels) == ["never", "video_transcode", "any_transcode", "playing"]


def test_plex_sessions_are_read(stub):
    stub.route("GET", "/status/sessions", {"MediaContainer": {"Metadata": [
        {"grandparentTitle": "Bluey", "title": "Keepy Uppy", "User": {"title": "kid"},
         "Player": {"state": "playing"},
         "Media": [{"Part": [{"decision": "directplay"}]}]},
        {"title": "Film", "User": {"title": "dad"}, "Player": {"state": "playing"},
         "TranscodeSession": {"videoDecision": "transcode", "audioDecision": "copy",
                              "transcodeHwEncoding": "nvenc"}},
        {"title": "Paused", "Player": {"state": "paused"},
         "TranscodeSession": {"videoDecision": "transcode"}},
    ]}})
    got = arr.plex_sessions(stub.url, "tok")
    assert [(s.who, s.what, s.video_decision) for s in got] == [
        ("kid", "Bluey - Keepy Uppy", "directplay"), ("dad", "Film", "transcode")]
    assert got[1].hardware
    assert stub.requests[0].headers["x-plex-token"] == "tok"


def test_jellyfin_sessions_are_read_with_the_mediabrowser_header(stub):
    stub.route("GET", "/Sessions", [
        {"UserName": "kid", "NowPlayingItem": {"Name": "Keepy Uppy", "SeriesName": "Bluey"},
         "PlayState": {"IsPaused": False}},
        {"UserName": "dad", "NowPlayingItem": {"Name": "Film"},
         "TranscodingInfo": {"IsVideoDirect": False, "IsAudioDirect": True,
                             "HardwareAccelerationType": "nvenc"}},
        {"UserName": "gran", "NowPlayingItem": {"Name": "Paused"},
         "PlayState": {"IsPaused": True}},
        {"UserName": "idle"},
    ])
    got = arr.jellyfin_sessions(stub.url, "key")
    assert [(s.who, s.what, s.video_decision, s.audio_decision) for s in got] == [
        ("kid", "Bluey - Keepy Uppy", "directplay", "directplay"),
        ("dad", "Film", "transcode", "copy")]
    auth = stub.requests[0].headers["authorization"]
    assert auth.startswith("MediaBrowser ") and 'Token="key"' in auth


def test_an_unreachable_server_never_holds_the_queue():
    assert arr.plex_sessions("http://127.0.0.1:9", "tok") == []
    assert arr.jellyfin_sessions("http://127.0.0.1:9", "key") == []
    assert arr.plex_sessions("", "tok") == []


def test_sessions_come_from_the_chosen_server_only(stub):
    stub.route("GET", "/status/sessions", {"MediaContainer": {"Metadata": [
        {"title": "Film", "Player": {"state": "playing"},
         "TranscodeSession": {"videoDecision": "transcode"}}]}})
    s = config.Settings(plex_url=stub.url, plex_token="t", library_source="plex")
    assert arr.sessions_for(s) == []               # media_server is "none"
    s.media_server = "plex"
    assert len(arr.sessions_for(s)) == 1


class _Stop(Exception):
    pass


def test_worker_holds_and_unloads_while_the_server_is_busy(home, settings, stub, monkeypatch):
    """One pass of the worker loop with a job queued and a transcode running."""
    from cleanarr import asr, db, pipeline, worker

    stub.route("GET", "/status/sessions", {"MediaContainer": {"Metadata": [
        {"title": "Film", "User": {"title": "dad"}, "Player": {"state": "playing"},
         "TranscodeSession": {"videoDecision": "transcode"}}]}})
    settings.media_server = "plex"
    settings.plex_url, settings.plex_token = stub.url, "t"
    config.save(settings)
    db.enqueue(kind="file", title="x", path="/tv/x.mkv")

    w = worker.Worker(home)
    unloaded, ran = [], []
    monkeypatch.setattr(asr, "unload_model", lambda: unloaded.append(1))
    monkeypatch.setattr(pipeline, "run_job", lambda *a, **k: ran.append(a[0]))

    def wait(timeout=None):
        w._stop.set()          # one pass is enough
        return True
    monkeypatch.setattr(w._wake, "wait", wait)
    w._loop()
    assert w.holding == "dad is watching Film (transcoding video)"
    assert unloaded and not ran

    # "Clean anyway" ignores the hold.
    w._stop.clear()
    w.override = True
    monkeypatch.setattr(pipeline, "run_job",
                        lambda *a, **k: (ran.append(a[0]), w._stop.set()))
    w._loop()
    assert ran and w.holding == ""
