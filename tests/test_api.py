"""The HTTP API: settings, the path check, posters and the queue endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from cleanarr import config, db, main, pipeline


# ---------------------------------------------------------------- settings

def test_secrets_are_masked_and_a_masked_save_keeps_them(client, settings):
    settings.sonarr = config.ArrConfig(url="http://s", api_key="real-key")
    settings.plex_token = "plex-secret"
    settings.jellyfin_api_key = "jf-secret"
    settings.asr_api_key = "asr-secret"
    config.save(settings)
    data = client.get("/api/settings").json()
    assert data["sonarr"]["api_key"] == "********"
    assert data["plex_token"] == "********"
    client.put("/api/settings", json=data)
    s = config.load()
    assert (s.sonarr.api_key, s.plex_token, s.jellyfin_api_key, s.asr_api_key) == (
        "real-key", "plex-secret", "jf-secret", "asr-secret")
    client.put("/api/settings", json={"plex_token": "new-token"})
    assert config.load().plex_token == "new-token"


def test_renaming_the_track_remembers_the_old_name(client, settings):
    client.put("/api/settings", json={"track_title": "English - Censored"})
    client.put("/api/settings", json={"track_title": "Family"})
    s = config.load()
    assert s.track_title == "Family"
    assert s.known_track_titles == ["Cleaned - English", "English - Censored"]
    client.put("/api/settings", json={"track_title": "  "})
    assert config.load().track_title == "Cleaned - English"


def test_old_config_keys_are_migrated(home):
    config.CONFIG_FILE.write_text(
        "pause_while_plex_playing: true\npath_map: [{from: /a, to: /b}]\n"
        "plex_url: http://p\nsonarr: {url: http://s, api_key: k}\n", encoding="utf8")
    s = config.load()
    assert s.hold_policy == "video_transcode"
    assert s.media_server == "plex"
    assert s.sonarr.api_key == "k"
    assert not hasattr(s, "path_map")


def test_categories_and_policies_are_offered(client, settings):
    data = client.get("/api/settings").json()
    assert [c["key"] for c in data["available_categories"]] == [
        "strong", "mild", "blasphemy", "slurs_sexual"]
    assert data["hold_policies"][0]["key"] == "never"


def test_service_tests_report_failures_as_data(client, settings):
    r = client.post("/api/settings/test/sonarr")
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "not configured"}
    r = client.post("/api/settings/test/plex")
    assert r.json()["ok"] is False
    assert client.post("/api/settings/test/nothing").status_code == 404


# ---------------------------------------------------------------- paths

def test_suggest_finds_the_folder_mounted_elsewhere(tmp_path):
    (tmp_path / "data" / "media" / "tv").mkdir(parents=True)
    root = str(tmp_path / "data")
    matched, found = main._suggest("/mnt/tank/media/tv", [root])
    assert found == str(Path(root, "media", "tv"))
    assert matched == "/mnt/tank/media/tv"


def test_suggest_needs_two_components_to_match(tmp_path):
    (tmp_path / "data" / "tv").mkdir(parents=True)
    # Only "tv" in common: a coincidence, not an answer.
    assert main._suggest("/mnt/other/tv", [str(tmp_path / "data")]) == ("", "")
    # A one-part path is all there is to match.
    (tmp_path / "data" / "movies").mkdir()
    assert main._suggest("/movies", [str(tmp_path / "data")])[1].endswith("movies")


def test_suggest_prefers_the_deepest_match(tmp_path):
    (tmp_path / "r" / "media" / "tv" / "Show").mkdir(parents=True)
    _, found = main._suggest("/x/media/tv/Show", [str(tmp_path / "r")])
    assert found.endswith("media/tv/Show")


def test_missing_explains_which_problem_it_is(tmp_path):
    assert "not found from this container" in pipeline._missing(tmp_path / "nope" / "a.mkv")
    assert "is not there any more" in pipeline._missing(tmp_path / "a.mkv")


def test_path_report(client, settings, stub, tmp_path, monkeypatch):
    here = tmp_path / "media" / "tv"
    here.mkdir(parents=True)
    stub.route("GET", "/api/v3/rootfolder", [{"path": str(here)}, {"path": "/nas/media/tv"}])
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    monkeypatch.setattr(main, "_visible_roots", lambda: [str(tmp_path)])
    rows = client.get("/api/paths").json()["roots"]
    assert rows[0]["ok"] is True
    assert rows[1]["ok"] is False and rows[1]["elsewhere"] == str(here)


# ---------------------------------------------------------------- posters

def test_sonarr_poster_url(client, settings, stub):
    stub.route("GET", "/api/v3/mediacover/42/poster.jpg", lambda req: (200, b"JPEGDATA"))
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    r = client.get("/api/poster", params={"source": "sonarr", "id": "42"})
    assert r.status_code == 200 and r.content == b"JPEGDATA"
    assert stub.requests[0].headers["x-api-key"] == "k"
    # Served from disk the second time.
    client.get("/api/poster", params={"source": "sonarr", "id": "42"})
    assert len(stub.requests) == 1
    assert client.get("/api/poster", params={"source": "sonarr", "id": "43"}).status_code == 404


# ---------------------------------------------------------------- jobs

def item(n: int) -> dict:
    return {"kind": "episode", "title": "Show", "subtitle": f"S01E0{n}",
            "path": f"/tv/show/{n}.mkv", "source": "sonarr", "source_id": str(n)}


def test_queueing_is_manual_and_marks_new_only(client, settings):
    r = client.post("/api/jobs", json={
        "items": [item(1), item(2), item(1)],
        "monitor": {"source": "sonarr", "source_id": "7", "title": "Show"}})
    assert r.json()["queued"] == 2 and r.json()["already_queued"] == 1
    assert [dict(m)["mode"] for m in db.monitors()] == ["new_only"]
    assert client.post("/api/jobs", json={"items": []}).status_code == 400


def test_queue_endpoints(client, settings):
    ids = client.post("/api/jobs", json={"items": [item(1), item(2), item(3)]}).json()["ids"]
    assert client.post(f"/api/jobs/{ids[2]}/move", json={"where": "top"}).status_code == 200
    assert client.post(f"/api/jobs/{ids[2]}/move", json={"where": "up"}).status_code == 409
    assert client.post("/api/jobs/move", json={"ids": [ids[0]], "where": "bottom"}).json()["moved"] == 1
    assert [r["id"] for r in db.queued_in_order()] == [ids[2], ids[1], ids[0]]
    assert client.post("/api/jobs/cancel", json={"ids": [ids[1]]}).json() == {"cancelled": 1}
    assert client.delete(f"/api/jobs/{ids[1]}").status_code == 409
    listing = client.get("/api/jobs").json()
    assert listing["cancelled"] == 1
    assert client.post("/api/jobs/retry", json={"ids": [ids[1]]}).json() == {"retrying": 1}
    assert client.delete("/api/queue").json() == {"cancelled": 3}
    assert client.delete("/api/jobs/cancelled").json() == {"removed": 3}
    assert client.get(f"/api/jobs/{ids[0]}").status_code == 404


def test_job_detail_lists_detections(client, settings):
    from cleanarr import words
    job = db.enqueue(kind="file", title="x", path="/tv/x.mkv")
    db.save_detections(job, [words.Match(1.0, 1.2, "shit", "strong")])
    got = client.get(f"/api/jobs/{job}").json()
    assert got["job"]["id"] == job
    assert got["detections"][0]["text"] == "shit"


def test_remove_all_needs_the_phrase(client, settings):
    assert client.post("/api/history/remove-all", json={}).status_code == 400
    job = db.enqueue(kind="file", title="x", path="/tv/x.mkv")
    db.update(job, status="done")
    assert client.post("/api/history/remove-all", json={"confirm": "remove all"}
                       ).json() == {"queued": 1}
    assert db.next_queued()["action"] == "remove"


def test_webhook_test_event(client, settings):
    r = client.post("/api/webhook/sonarr", json={"eventType": "Test"})
    assert r.json()["message"] == "Cleanarr heard you"


def test_file_info(client, settings, tmp_path):
    assert client.get("/api/file", params={"path": str(tmp_path / "no.mkv")}).status_code == 404
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"x")
    assert client.get("/api/file", params={"path": str(junk)}).status_code == 400


def test_static_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/static/sw.js").headers["service-worker-allowed"] == "/"
