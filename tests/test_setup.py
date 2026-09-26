"""The first-run checklist on Home."""

from __future__ import annotations

from cleanarr import config, db, main


def states(client) -> dict:
    data = client.get("/api/setup").json()
    return {i["key"]: (i["state"], i["detail"]) for i in data["required"] + data["optional"]}


def test_a_fresh_install_has_everything_to_do(client, settings):
    got = states(client)
    assert got["library"][0] == "todo"
    assert "Sonarr or Radarr" in got["library"][1]
    assert got["paths"][0] == "todo"
    assert got["listening"][0] == "todo"
    assert got["first_clean"][0] == "todo"
    assert got["login"][0] == "info"
    assert "judge" in got                   # default words listed, no judge set
    assert client.get("/api/setup").json()["done"] is False


def test_a_finished_install(client, settings, stub, tmp_path, monkeypatch):
    media_dir = tmp_path / "tv"
    media_dir.mkdir()
    stub.route("GET", "/api/v3/system/status", {"appName": "Sonarr", "version": "4"})
    stub.route("GET", "/api/v3/rootfolder", [{"path": str(media_dir)}])
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    settings.check_in_context = []
    config.save(settings)
    monkeypatch.setattr(main, "_is_complete", lambda name: True)
    job = db.enqueue(kind="file", title="x", path="/x")
    db.update(job, status="done")
    got = states(client)
    assert got["library"] == ("ok", "Sonarr answered. Radarr is not set, so films will not show.")
    assert got["paths"][0] == "ok"
    assert got["listening"][0] == "ok"
    assert got["first_clean"][0] == "ok"
    assert "judge" not in got
    assert client.get("/api/setup").json()["done"] is True
    client.post("/api/auth/setup", json={"username": "amy", "password": "long enough"})
    assert states(client)["login"][0] == "ok"


def test_problems_are_named(client, settings, stub, monkeypatch):
    stub.route("GET", "/api/v3/system/status", lambda req: (401, {}))
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="bad")
    settings.device = "cuda"
    config.save(settings)
    monkeypatch.setattr("cleanarr.asr._cuda_available", lambda: False)
    got = states(client)
    assert got["library"] == ("problem", "Sonarr rejected the API key")
    assert got["listening"][0] == "problem"


def test_unreachable_folders(client, settings, stub):
    stub.route("GET", "/System/Info", {"ServerName": "jf", "Version": "10"})
    stub.route("GET", "/Library/VirtualFolders", [
        {"Name": "TV", "CollectionType": "tvshows", "Locations": ["/nowhere/tv"]}])
    settings.library_source = "jellyfin"
    settings.jellyfin_url, settings.jellyfin_api_key = stub.url, "k"
    config.save(settings)
    got = states(client)
    assert got["library"] == ("ok", "jf answered.")
    assert got["paths"][0] == "problem" and "/nowhere/tv" in got["paths"][1]


def test_remote_whisper(client, settings):
    settings.asr_backend = "remote"
    settings.asr_url = "http://whisper:8000"
    config.save(settings)
    assert states(client)["listening"][0] == "ok"


def test_model_download_error_is_shown(client, settings, monkeypatch):
    def fail(name, folder):
        raise OSError("Network is unreachable")
    monkeypatch.setattr(main, "_pull_model", fail)
    monkeypatch.setattr(main, "_expected_bytes", lambda name: 0)
    assert client.post("/api/models/medium.en/download").json() == {"started": True}
    import time
    for _ in range(50):
        if "medium.en" not in main._downloading and main._download_errors.get("medium.en"):
            break
        time.sleep(0.05)
    row = client.get("/api/models").json()["items"][0]
    assert "huggingface.co" in row["error"]
    assert states(client)["listening"][0] == "problem"
    main._download_errors.clear()
