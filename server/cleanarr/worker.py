"""The queue: one job at a time, forever.

One at a time is deliberate. The GPU is shared with Plex transcodes and
Ollama, and two jobs would each take twice as long while making the box worse
at the thing it is actually for.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import arr, asr, config, db, pipeline

# How long an idle queue holds the speech model before handing its ~2GB back.
# Was ten minutes, which left the card two gigabytes down long after the last
# episode finished - and on an 8GB card shared with Plex and Ollama that is the
# difference between the next thing fitting and not. Ninety seconds costs
# nothing in practice: a queue with work in it never reaches this branch, so
# back-to-back jobs still never reload. It only applies once the queue has
# actually drained, and reloading after that costs a few seconds, once.
IDLE_UNLOAD_SECONDS = 90
WATCHING_RECHECK_SECONDS = 60
MONITOR_INTERVAL_SECONDS = 600


def _arrived_after(added: str, since: float) -> bool:
    """Did Sonarr import this file after the show was marked?

    An unreadable or missing date counts as new, because the alternative is
    silently never cleaning an episode.
    """
    if not added:
        return True
    try:
        stamp = datetime.fromisoformat(str(added).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp() > since
    except (TypeError, ValueError):
        return True


class Worker:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self._thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel_current: int | None = None
        self.current_job: int | None = None
        self.holding: str = ""     # why the queue is waiting, for the UI
        self.last_monitor_check: float = 0.0
        self.last_monitor_queued: int = 0
        # "Clean anyway": ignore the hold until the queue runs dry, then go
        # back to normal. Deliberately not permanent - it answers "I know, do
        # it now", not "stop asking".
        self.override: bool = False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="cleanarr-worker",
                                        daemon=True)
        self._thread.start()
        # Watching for new episodes runs on its own thread, because the worker
        # thread spends most of its life inside a single job. Sharing them
        # meant an episode that landed during a long queue waited for the whole
        # queue before anyone noticed it - the opposite of "new episodes first".
        self._watch_thread = threading.Thread(target=self._watch_loop,
                                              name="cleanarr-monitor", daemon=True)
        self._watch_thread.start()

    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.check_monitored():
                    self.nudge()
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanarr] monitor check failed: {exc}", flush=True)
            self._stop.wait(timeout=MONITOR_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def nudge(self) -> None:
        """Something was queued; look again now rather than at the next tick."""
        self._wake.set()

    def cancel(self, job_id: int) -> bool:
        if self.current_job == job_id:
            self._cancel_current = job_id
            return True
        return db.cancel_queued(job_id)

    # -- the loop ---------------------------------------------------------
    # -- shows that clean themselves -------------------------------------
    def check_monitored(self) -> int:
        """Queue any episode of a monitored show that has not been cleaned.

        Sonarr is the source of truth for what exists; this service's own job
        history is the record of what has been done. Anything in the first and
        not the second gets queued - which covers an episode that arrived an
        hour ago and one that arrived while the container was off.
        """
        settings = config.load()
        self.last_monitor_check = time.time()
        watched = db.monitors("sonarr")
        if not watched or not settings.sonarr.url:
            return 0
        client = arr.Sonarr(settings.sonarr.url, settings.sonarr.api_key)
        known = db.job_paths()
        queued = 0
        for row in watched:
            try:
                episodes = client.episodes(int(row["source_id"]))
            except (arr.ArrError, ValueError):
                continue
            mode = (row["mode"] if "mode" in row.keys() else "catch_up") or "catch_up"
            for episode in episodes:
                if known.get(episode["path"]) in ("done", "skipped", "queued",
                                                  "running", "failed"):
                    continue
                # "From now on" means exactly that: files that were already on
                # disk when the show was marked are left alone.
                if mode == "new_only" and not _arrived_after(episode.get("added"),
                                                             row["added_at"]):
                    continue
                # An episode that just landed goes to the front: it is the one
                # someone is waiting to watch tonight, while a back-catalogue
                # queue can wait another hour.
                job_id = db.enqueue(
                    kind="episode", title=row["title"] or "Monitored show",
                    subtitle=f"S{episode['season']:02d}E{episode['episode']:02d}"
                             f" · {episode['title']}",
                    path=episode["path"], source="sonarr",
                    source_id=str(episode["id"]), front=True)
                if job_id:
                    queued += 1
            db.monitor_touch("sonarr", row["source_id"])
        self.last_monitor_queued = queued
        if queued:
            print(f"[cleanarr] queued {queued} new episode(s) from monitored shows",
                  flush=True)
        return queued

    def _loop(self) -> None:
        last_job_at = time.time()
        while not self._stop.is_set():
            job = db.next_queued()
            if job is None:
                self.holding = ""
                self.override = False      # nothing left to push through
                # Hand the VRAM back after a quiet spell.
                if time.time() - last_job_at > IDLE_UNLOAD_SECONDS:
                    asr.unload_model()
                    last_job_at = time.time()
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue

            # Plex is working for someone: leave the GPU alone and look again
            # shortly. A direct play is not a reason to wait - see should_hold.
            settings = config.load()
            if not self.override:
                sessions = arr.plex_sessions(settings.plex_url, settings.plex_token)
                hold, why = arr.should_hold(sessions, settings.hold_policy)
                if hold:
                    self.holding = why
                    asr.unload_model()
                    self._wake.wait(timeout=WATCHING_RECHECK_SECONDS)
                    self._wake.clear()
                    continue
            self.holding = ""

            job_id = int(job["id"])
            self.current_job = job_id
            self._cancel_current = None
            try:
                pipeline.run_job(
                    job_id, settings, self.cache_dir,
                    should_cancel=lambda: self._cancel_current == job_id
                    or self._stop.is_set())
            finally:
                self.current_job = None
                last_job_at = time.time()

    @property
    def busy(self) -> bool:
        return self.current_job is not None
