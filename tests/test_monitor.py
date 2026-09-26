"""Shows that clean themselves: catch_up vs new_only, and what counts as done."""

from __future__ import annotations

import time

import pytest

from cleanarr import config, db, worker


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@pytest.fixture
def sonarr(stub, settings):
    now = time.time()
    stub.route("GET", "/api/v3/episode", [
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "Old", "episodeFileId": 11},
        {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "title": "New", "episodeFileId": 12},
        {"id": 3, "seasonNumber": 1, "episodeNumber": 3, "title": "Undated", "episodeFileId": 13},
        {"id": 4, "seasonNumber": 1, "episodeNumber": 4, "title": "Not aired"},
    ])
    stub.route("GET", "/api/v3/episodefile", [
        {"id": 11, "path": "/tv/show/s01e01.mkv", "dateAdded": iso(now - 86400 * 30)},
        {"id": 12, "path": "/tv/show/s01e02.mkv", "dateAdded": iso(now + 60)},
        {"id": 13, "path": "/tv/show/s01e03.mkv", "dateAdded": ""},
    ])
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k", enabled=True)
    config.save(settings)
    return stub


def queued_titles() -> list[str]:
    return [db.get(r["id"])["subtitle"] for r in db.queued_in_order()]


def test_catch_up_queues_everything_on_disk(sonarr, home):
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    w = worker.Worker(home)
    assert w.check_monitored() == 3
    assert sorted(queued_titles()) == ["S01E01 · Old", "S01E02 · New", "S01E03 · Undated"]
    assert sonarr.requests[0].headers["x-api-key"] == "k"
    assert db.monitors("sonarr")[0]["last_checked"] > 0


def test_new_only_leaves_the_back_catalogue_alone(sonarr, home):
    db.monitor_add("sonarr", "7", "Show", "new_only")
    assert worker.Worker(home).check_monitored() == 2
    # An undated file counts as new: never silently skipping one is the rule.
    assert sorted(queued_titles()) == ["S01E02 · New", "S01E03 · Undated"]


def test_nothing_is_queued_twice_or_after_it_finished(sonarr, home):
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    w = worker.Worker(home)
    w.check_monitored()
    assert w.check_monitored() == 0
    for row in db.queued_in_order():
        db.update(row["id"], status="done")
    assert w.check_monitored() == 0


def test_a_failed_episode_waits_for_a_person(sonarr, home):
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    db.enqueue(kind="episode", title="Show", path="/tv/show/s01e01.mkv")
    db.update(db.next_queued()["id"], status="failed")
    assert worker.Worker(home).check_monitored() == 2


def test_new_episodes_go_to_the_front(sonarr, home):
    db.enqueue(kind="episode", title="Backlog", subtitle="backlog", path="/tv/other.mkv")
    db.monitor_add("sonarr", "7", "Show", "new_only")
    worker.Worker(home).check_monitored()
    assert queued_titles()[-1] == "backlog"


def test_cleaning_a_season_does_not_downgrade_catch_up(home):
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    db.monitor_add("sonarr", "7", "Show", "new_only", keep_existing_mode=True)
    assert db.monitors()[0]["mode"] == "catch_up"
    db.monitor_add("sonarr", "7", "Show", "new_only")
    assert db.monitors()[0]["mode"] == "new_only"


def test_an_unreachable_library_is_skipped_quietly(home, settings):
    settings.sonarr = config.ArrConfig(url="http://127.0.0.1:9", api_key="k")
    config.save(settings)
    db.monitor_add("sonarr", "7", "Show", "catch_up")
    assert worker.Worker(home).check_monitored() == 0


def test_no_monitors_means_no_requests(home, settings, stub):
    settings.sonarr = config.ArrConfig(url=stub.url, api_key="k")
    config.save(settings)
    assert worker.Worker(home).check_monitored() == 0
    assert stub.requests == []


def test_plex_library_monitors_work_the_same(home, settings, stub):
    now = time.time()
    stub.route("GET", "/library/metadata/55/allLeaves", {"MediaContainer": {"Metadata": [
        {"ratingKey": "1", "parentIndex": 2, "index": 1, "title": "Old",
         "addedAt": int(now - 86400), "Media": [{"Part": [{"file": "/tv/a.mkv"}]}]},
        {"ratingKey": "2", "parentIndex": 2, "index": 2, "title": "New",
         "addedAt": int(now + 60), "Media": [{"Part": [{"file": "/tv/b.mkv"}]}]},
    ]}})
    settings.library_source = "plex"
    settings.plex_url, settings.plex_token = stub.url, "t"
    config.save(settings)
    db.monitor_add("sonarr", "55", "Show", "new_only")
    assert worker.Worker(home).check_monitored() == 1
    assert queued_titles() == ["S02E02 · New"]


@pytest.mark.parametrize("added, since, want", [
    ("", 100.0, True),
    ("garbage", 100.0, True),
    ("1970-01-01T00:01:00Z", 100.0, False),
    ("1970-01-01T00:01:41Z", 100.0, True),
    ("1970-01-01T00:01:41", 100.0, True),          # naive means UTC
])
def test_arrived_after(added, since, want):
    assert worker._arrived_after(added, since) is want
