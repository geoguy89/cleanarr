"""The library clients, against stand-ins for Sonarr, Radarr, Plex and Jellyfin."""

from __future__ import annotations

import pytest

from cleanarr import arr, config, library


# ---------------------------------------------------------------- Sonarr / Radarr

def test_sonarr_episodes_join_files(stub):
    stub.route("GET", "/api/v3/episode", [
        {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "title": "B", "episodeFileId": 20},
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "A", "episodeFileId": 10},
        {"id": 3, "seasonNumber": 1, "episodeNumber": 3, "title": "C"},
    ])
    stub.route("GET", "/api/v3/episodefile", [
        {"id": 10, "path": "/tv/a.mkv", "size": 5, "dateAdded": "2024-01-01T00:00:00Z",
         "quality": {"quality": {"name": "HDTV-720p"}}},
        {"id": 20, "path": "/tv/b.mkv", "size": 6},
    ])
    got = arr.Sonarr(stub.url, "k").episodes(7)
    assert [(e["id"], e["path"]) for e in got] == [(1, "/tv/a.mkv"), (2, "/tv/b.mkv")]
    assert got[0]["quality"] == "HDTV-720p"
    assert stub.requests[0].query["seriesId"] == ["7"]


def test_arr_errors_are_plain(stub):
    stub.route("GET", "/api/v3/system/status", lambda req: (401, {}))
    with pytest.raises(arr.ArrError, match="API key was rejected"):
        arr.Sonarr(stub.url, "bad").test()
    with pytest.raises(arr.ArrError, match="not configured"):
        arr.Sonarr("", "").test()
    with pytest.raises(arr.ArrError, match="could not reach"):
        arr.Sonarr("http://127.0.0.1:9", "k").test()


def test_radarr_skips_films_with_no_file(stub):
    stub.route("GET", "/api/v3/movie", [
        {"id": 1, "title": "b film", "movieFile": {"path": "/m/b.mkv"}},
        {"id": 2, "title": "A film", "movieFile": {"path": "/m/a.mkv"}},
        {"id": 3, "title": "wanted"},
    ])
    assert [m["title"] for m in arr.Radarr(stub.url, "k").movies()] == ["A film", "b film"]


def test_arr_library_roots(stub):
    stub.route("GET", "/api/v3/rootfolder", [{"path": "/data/tv"}])
    lib = library.ArrLibrary(arr.Sonarr(stub.url, "k"), arr.Radarr("", ""))
    assert lib.roots() == [{"library": "/data/tv", "kind": "show", "path": "/data/tv"}]


# ---------------------------------------------------------------- Plex

def test_plex_pages_through_a_section(stub, monkeypatch):
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "1", "type": "show", "title": "TV", "Location": [{"path": "/data/tv"}]},
        {"key": "2", "type": "artist", "title": "Music"}]}})
    shows = [{"ratingKey": str(i), "title": f"S{i}", "leafCount": i} for i in range(5)]

    def page(req):
        start = int(req.query["X-Plex-Container-Start"][0])
        size = int(req.query["X-Plex-Container-Size"][0])
        return 200, {"MediaContainer": {"totalSize": 5, "Metadata": shows[start:start + size]}}
    stub.route("GET", "/library/sections/1/all", page)
    real = library.PlexLibrary._all

    def small_pages(self, section, **params):
        # Same loop, page size 2, to exercise paging without 500 items.
        out, start = [], 0
        while True:
            c = self._get(f"/library/sections/{section}/all",
                          **{**params, "X-Plex-Container-Start": start,
                             "X-Plex-Container-Size": 2})
            batch = c.get("Metadata") or []
            out.extend(batch)
            start += 2
            if len(batch) < 2 or start >= int(c.get("totalSize") or len(out)):
                return out
    monkeypatch.setattr(library.PlexLibrary, "_all", small_pages)
    got = library.PlexLibrary(stub.url, "tok").shows()
    assert [s["title"] for s in got] == [f"S{i}" for i in range(5)]
    assert all(r.query["X-Plex-Token"] == ["tok"] for r in stub.requests)
    monkeypatch.setattr(library.PlexLibrary, "_all", real)
    assert library.PlexLibrary(stub.url, "tok").roots() == [
        {"library": "TV", "kind": "show", "path": "/data/tv"}]


def test_plex_errors(stub):
    stub.route("GET", "/library/sections", lambda req: (401, {}))
    with pytest.raises(library.LibraryError, match="rejected the token"):
        library.PlexLibrary(stub.url, "bad").test()
    with pytest.raises(library.LibraryError, match="not set"):
        library.PlexLibrary("", "").test()


def test_plex_episodes_skip_missing_files(stub):
    stub.route("GET", "/library/metadata/9/allLeaves", {"MediaContainer": {"Metadata": [
        {"ratingKey": "2", "parentIndex": 1, "index": 2, "title": "B",
         "Media": [{"Part": [{"file": "/tv/b.mkv", "size": 3}]}]},
        {"ratingKey": "1", "parentIndex": 1, "index": 1, "title": "A",
         "Media": [{"Part": [{"file": "/tv/a.mkv"}]}]},
        {"ratingKey": "3", "parentIndex": 1, "index": 3, "title": "no file"},
    ]}})
    got = library.PlexLibrary(stub.url, "t").episodes("9")
    assert [e["title"] for e in got] == ["A", "B"]


# ---------------------------------------------------------------- Jellyfin

def test_jellyfin_uses_the_mediabrowser_header(stub):
    stub.route("GET", "/System/Info", {"ServerName": "jf", "Version": "10.9"})
    assert library.JellyfinLibrary(stub.url, "key").test() == {
        "ok": True, "app": "jf", "version": "10.9"}
    headers = stub.requests[0].headers
    assert headers["authorization"].startswith('MediaBrowser Token="key"')


def test_jellyfin_errors(stub):
    stub.route("GET", "/System/Info", lambda req: (401, {}))
    with pytest.raises(library.LibraryError, match="rejected the API key"):
        library.JellyfinLibrary(stub.url, "bad").test()


def test_jellyfin_episodes_and_movies(stub):
    def items(req):
        kind = req.query["IncludeItemTypes"][0]
        if kind == "Episode":
            return 200, {"TotalRecordCount": 2, "Items": [
                {"Id": "e2", "ParentIndexNumber": 1, "IndexNumber": 2, "Name": "B",
                 "Path": "/tv/b.mkv", "DateCreated": "2024-02-02T10:00:00.0000000Z"},
                {"Id": "e1", "ParentIndexNumber": 1, "IndexNumber": 1, "Name": "A",
                 "Path": "/tv/a.mkv", "MediaSources": [{"Size": 7}]}]}
        return 200, {"TotalRecordCount": 1, "Items": [
            {"Id": "m1", "Name": "Film", "Path": "/m/f.mkv"}]}
    stub.route("GET", "/Items", items)
    lib = library.JellyfinLibrary(stub.url, "k")
    eps = lib.episodes("abc")
    assert [e["id"] for e in eps] == ["e1", "e2"]
    assert eps[1]["added"] == "2024-02-02T10:00:00"
    assert eps[0]["size"] == 7
    assert [m["title"] for m in lib.movies()] == ["Film"]


def test_jellyfin_roots(stub):
    stub.route("GET", "/Library/VirtualFolders", [
        {"Name": "TV", "CollectionType": "tvshows", "Locations": ["/data/tv"]},
        {"Name": "Music", "CollectionType": "music", "Locations": ["/data/music"]}])
    assert library.JellyfinLibrary(stub.url, "k").roots() == [
        {"library": "TV", "kind": "show", "path": "/data/tv"}]


def test_build_picks_the_source():
    s = config.Settings()
    assert library.build(s).name == "arr"
    s.library_source = "plex"
    assert isinstance(library.build(s), library.PlexLibrary)
    s.library_source = "jellyfin"
    assert isinstance(library.build(s), library.JellyfinLibrary)
    assert not library.build(s).has_calendar


# ---------------------------------------------------------------- refresh

def test_plex_refresh_targets_the_right_section(stub):
    stub.route("GET", "/library/sections", {"MediaContainer": {"Directory": [
        {"key": "3", "title": "TV", "Location": [{"path": "/data/tv"}]}]}})
    stub.route("GET", "/library/sections/3/refresh", {})
    assert arr.plex_refresh(stub.url, "t", "/data/tv/show/e1.mkv") == "Plex: refreshed TV"
    assert stub.seen("/library/sections/3/refresh")[0].query["path"] == ["/data/tv/show"]
    assert "no library" in arr.plex_refresh(stub.url, "t", "/elsewhere/x.mkv")


def test_jellyfin_refresh_names_the_file(stub):
    stub.route("POST", "/Library/Media/Updated", {})
    assert "told it" in arr.jellyfin_refresh(stub.url, "k", "/tv/a.mkv")
    req = stub.seen("/Library/Media/Updated")[0]
    assert req.json() == {"Updates": [{"Path": "/tv/a.mkv", "UpdateType": "Modified"}]}
    assert req.headers["authorization"].startswith("MediaBrowser")


def test_refresh_target_falls_back_to_the_library():
    s = config.Settings()
    assert arr.refresh_target(s) == "none"
    s.library_source = "plex"
    assert arr.refresh_target(s) == "plex"
    s.media_server = "jellyfin"
    assert arr.refresh_target(s) == "jellyfin"
    assert arr.refresh_for(config.Settings(), "/x") == "no media server configured"
