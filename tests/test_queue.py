"""The queue: order, moves, dedupe, requeue after a restart, retries."""

from __future__ import annotations

import sqlite3

import pytest

from cleanarr import db, words


def q(path: str, **kw) -> int | None:
    return db.enqueue(kind="episode", title=path, path=f"/tv/{path}.mkv", **kw)


def order() -> list[str]:
    return [db.get(r["id"])["title"] for r in db.queued_in_order()]


def test_runs_in_the_order_queued(home):
    for name in "abc":
        q(name)
    assert order() == ["a", "b", "c"]
    assert db.next_queued()["title"] == "a"


def test_front_goes_first(home):
    q("a"), q("b")
    q("new", front=True)
    assert order() == ["new", "a", "b"]


def test_a_file_already_waiting_is_not_queued_twice(home):
    assert q("a") is not None
    assert q("a") is None
    db.update(db.next_queued()["id"], status="done")
    assert q("a") is not None          # finished, so it can be queued again


@pytest.mark.parametrize("pick, where, want", [
    (["c"], "top", ["c", "a", "b", "d", "e"]),
    (["b"], "bottom", ["a", "c", "d", "e", "b"]),
    (["c"], "up", ["a", "c", "b", "d", "e"]),
    (["c"], "down", ["a", "b", "d", "c", "e"]),
    (["b", "d"], "up", ["b", "d", "a", "c", "e"]),
    (["b", "d"], "down", ["a", "c", "e", "b", "d"]),
    (["d", "b"], "top", ["b", "d", "a", "c", "e"]),   # queue order, not click order
])
def test_moves(home, pick, where, want):
    ids = {name: q(name) for name in "abcde"}
    assert db.move_many([ids[p] for p in pick], where) == len(pick)
    assert order() == want


def test_moves_that_cannot_happen(home):
    ids = {name: q(name) for name in "abc"}
    assert db.move(ids["a"], "up") == 0
    assert db.move(ids["c"], "down") == 0
    assert db.move(ids["b"], "sideways") == 0
    assert db.move_many([], "top") == 0
    assert order() == ["a", "b", "c"]


def test_front_still_works_after_renumbering(home):
    ids = {name: q(name) for name in "abc"}
    db.move(ids["c"], "top")
    q("urgent", front=True)
    q("later")
    assert order() == ["urgent", "c", "a", "b", "later"]


def test_requeue_interrupted(home):
    a, b = q("a"), q("b")
    db.update(a, status="running", stage="listening", progress=0.4)
    assert db.requeue_interrupted() == 1
    row = db.get(a)
    assert row["status"] == "queued" and row["progress"] == 0 and row["stage"] == ""
    assert "restarted" in row["message"]
    assert db.get(b)["status"] == "queued"
    assert db.requeue_interrupted() == 0


def test_requeued_job_keeps_its_place(home):
    a, _b = q("a"), q("b")
    db.update(a, status="running")
    db.requeue_interrupted()
    assert order() == ["a", "b"]


def test_retry_puts_failed_and_cancelled_at_the_back(home):
    a, b, c = q("a"), q("b"), q("c")
    db.update(a, status="failed", message="boom", finished_at=1.0)
    db.cancel_queued(b)
    assert db.retry_failed() == 2
    assert order() == ["c", "a", "b"]
    assert db.get(a)["finished_at"] is None
    assert db.retry_failed([c]) == 0          # not failed


def test_cancel(home):
    a, b, c = q("a"), q("b"), q("c")
    assert db.cancel_queued(a)
    assert not db.cancel_queued(a)
    assert db.cancel_many([b, 999]) == 1
    assert db.cancel_all_queued() == 1
    assert db.get(c)["status"] == "cancelled"
    assert db.purge(("cancelled",)) == 3
    assert db.get(a) is None


def test_recent_lists_running_then_queue_then_failures(home):
    a, b, c, d = q("a"), q("b"), q("c"), q("d")
    db.update(c, status="running")
    db.update(d, status="failed")
    db.update(a, status="done")
    assert [r["title"] for r in db.recent()] == ["c", "b", "d"]
    assert "a" in [r["title"] for r in db.recent(include_finished=True)]


def test_history_stats_and_forgetting(home):
    a = q("a")
    db.update(a, status="done", muted=3, added_bytes=1000, finished_at=5.0)
    b = q("b")
    db.update(b, status="skipped", finished_at=6.0)
    assert [r["title"] for r in db.history()] == ["b", "a"]
    assert [r["title"] for r in db.history("a")] == ["a"]
    assert db.stats() == {"cleaned_files": 1, "words_muted": 3, "waiting": 0,
                          "added_bytes": 1000}
    assert db.cleaned_before("/tv/a.mkv", job_id=0)
    assert not db.cleaned_before("/tv/a.mkv", job_id=a)
    assert db.forget_cleaned("/tv/a.mkv") == 1
    assert not db.cleaned_before("/tv/a.mkv")


def test_cleaned_paths_reports_latest(home):
    a = q("a")
    db.update(a, status="done", muted=2, finished_at=1.0)
    out = db.cleaned_paths(["/tv/a.mkv", "/tv/none.mkv"])
    assert out["/tv/a.mkv"]["status"] == "done"
    assert out["/tv/a.mkv"]["muted"] == 2
    assert "/tv/none.mkv" not in out
    assert db.cleaned_paths([]) == {}


def test_detections_round_trip(home):
    a = q("a")
    muted = [words.Match(1.0, 1.2, "shit", "strong")]
    left = [words.Match(2.0, 2.2, "cock", "slurs_sexual", True, "left in: heard as caulk")]
    db.save_detections(a, muted, left)
    rows = db.detections(a)
    assert [(r["text"], r["muted"]) for r in rows] == [("shit", 1), ("cock", 0)]
    db.save_detections(a, [], [])
    assert db.detections(a) == []


def test_old_database_gets_new_columns(home):
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript("""
        CREATE TABLE job (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          title TEXT NOT NULL, subtitle TEXT DEFAULT '', path TEXT NOT NULL,
          source TEXT DEFAULT '', source_id TEXT DEFAULT '',
          status TEXT NOT NULL DEFAULT 'queued', stage TEXT DEFAULT '',
          progress REAL DEFAULT 0, message TEXT DEFAULT '', muted INTEGER DEFAULT 0,
          duration REAL DEFAULT 0, created_at REAL NOT NULL, started_at REAL,
          finished_at REAL);
        INSERT INTO job (kind, title, path, created_at) VALUES ('episode','old','/x',1);
    """)
    conn.commit()
    conn.close()
    row = db.get(1)
    for column in ("force", "position", "action", "added_bytes"):
        assert column in row.keys()
    assert q("new") is not None
