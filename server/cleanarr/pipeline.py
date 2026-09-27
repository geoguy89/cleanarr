"""One file, start to finish.

    probe -> extract -> listen -> match -> mute -> add track -> verify -> swap

Every step writes its progress to the job row, because the only thing worse
than a slow job is a slow job that looks stuck. Nothing touches the original
file until the last step, and that step refuses to run on a file that did not
verify.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from . import arr, asr, config, db, judge, library, media, subtitles, words

# What each stage is worth, so the bar moves smoothly across the whole job
# rather than sitting at 0% through the part that takes the longest.
# Shares of the bar, roughly proportional to how long each stage really takes.
# Judging used to get two percent of the bar while taking most of the minutes,
# so a job in that stage looked frozen - which is exactly how it was reported.
STAGES = {
    "probing": (0.00, 0.02),
    "extracting": (0.02, 0.05),
    "listening": (0.05, 0.45),
    "matching": (0.45, 0.46),
    "judging": (0.46, 0.88),
    # Muting and remuxing are one ffmpeg pass now; "remuxing" is still its own
    # stage for a removal, which has nothing to mute.
    "muting": (0.88, 0.97),
    "remuxing": (0.88, 0.97),
    "verifying": (0.97, 1.00),
}


def _missing(path: Path) -> str:
    """Why a file could not be opened, in the two cases that mean it.

    A file that was cleaned last week and has since been deleted is a
    different problem from a library folder that was never mounted here, and
    "is not there any more" is wrong about the second - which is the one a new
    install hits on every single job.
    """
    folder = path.parent
    if not folder.is_dir():
        return (f"{path} not found from this container - it cannot see "
                f"{folder}. Check Settings → Your library.")
    return f"{path} is not there any more"


class Cancelled(Exception):
    pass


class Pipeline:
    def __init__(self, settings: config.Settings, cache_dir: Path,
                 should_cancel=None):
        self.settings = settings
        self.cache_dir = cache_dir
        self.should_cancel = should_cancel or (lambda: False)
        self.job_id: int | None = None

    # -- progress plumbing ------------------------------------------------
    def _stage(self, job_id: int, stage: str, message: str = "") -> None:
        if self.should_cancel():
            raise Cancelled()
        self.job_id = job_id
        low, _high = STAGES[stage]
        db.update(job_id, stage=stage, progress=low, message=message)

    def _progress(self, job_id: int, stage: str):
        low, high = STAGES[stage]
        last = [0.0]

        def report(fraction: float) -> None:
            if self.should_cancel():
                raise Cancelled()
            value = low + (high - low) * max(0.0, min(1.0, fraction))
            if value - last[0] >= 0.005:      # don't write to sqlite every frame
                last[0] = value
                db.update(job_id, progress=round(value, 4))
        return report

    # -- second opinion ---------------------------------------------------
    def _adjudicate(self, matches: list, transcript_words: list[dict],
                    settings: config.Settings) -> tuple[list, list]:
        """Split matches into (mute, leave alone), asking Ollama about the
        ambiguous ones. Anything it cannot answer stays muted."""
        if not settings.judge_url:
            return matches, []

        # Which words are worth asking about is the household's call, not the
        # code's: the setting is the whole list, and an empty one means nothing
        # is asked. A word taken off the list is simply muted.
        checked = {w.strip().lower() for w in settings.check_in_context if w.strip()}
        ambiguous = [(i, m) for i, m in enumerate(matches)
                     if judge.is_ambiguous(m.text, checked)]
        if not ambiguous:
            return matches, []

        # The same word said repeatedly in one scene is one decision, not
        # twelve. Grouping pools the context - the drywall scene that produced
        # nine "cock" mutes only reads as caulk once you see several of them
        # together - and keeps the answers consistent across the scene.
        groups = judge.group(ambiguous, transcript_words)
        lines = [{"n": g.n, "line": g.line} for g in groups]

        # Give the speech model's VRAM back before asking Ollama anything. On
        # an 8GB card those two gigabytes are the difference between the judge
        # running on the GPU and Ollama quietly putting half its layers on the
        # CPU: measured, that was 230 seconds with eight cores pegged against
        # 25 seconds with the card to itself. Reloading Whisper for the next
        # job costs a few seconds.
        asr.unload_model()

        low, high = STAGES["judging"]

        def say(done: int, total: int, word: str) -> None:
            if self.should_cancel():
                raise Cancelled()
            db.update(self.job_id,
                      progress=round(low + (high - low) * (done / max(total, 1)), 4),
                      message=f"second opinion {done + 1} of {total}: is “{word}” "
                              f"profanity here? (each takes 30-100s)")

        verdicts = judge.adjudicate(
            lines, settings.judge_url, settings.judge_model,
            threads=settings.judge_threads, keep_alive=settings.judge_keep_alive,
            progress=say)

        mute, leave = [], []
        lookup = {g.n: g.indexes for g in groups}
        cleared = {i: instead for n, (verdict, instead) in verdicts.items()
                   if verdict == "CLEAN" for i in lookup.get(n, ())}
        asked = {i for g in groups for i in g.indexes}
        for i, match in enumerate(matches):
            if i in cleared:
                heard = cleared[i]
                leave.append(replace(
                    match, needs_review=True,
                    reason=("left in: reverent, not an exclamation"
                            if heard.lower().startswith("reverent")
                            else f"left in: heard as “{heard}” here")))
            else:
                if i in asked and verdicts:
                    match = replace(
                        match, needs_review=True,
                        reason=match.reason or "ambiguous word, judged profanity here")
                mute.append(match)
        return mute, leave

    # -- whose track is that? ---------------------------------------------
    def _ours(self, original, path: Path, job_id: int) -> tuple[int, ...]:
        """The audio streams this service can show it wrote.

        Dropping a track is the only destructive thing here, so it is not done
        on a name match alone: a track carries the mark, or the handler name
        only this writes, or there is a finished job for this file. A track
        that merely shares the configured name belongs to the file.
        """
        seen = db.cleaned_before(str(path), job_id)
        return tuple(a.audio_index for a in original.audio
                     if a.is_cleaned and (a.written_here or seen))

    # -- taking it back out -----------------------------------------------
    def remove(self, job_id: int, path: Path) -> dict:
        """Write the file back without its cleaned track, freeing the space."""
        settings = self.settings
        if not path.exists():
            raise FileNotFoundError(_missing(path))

        self._stage(job_id, "probing", "reading the file")
        original = media.probe(path)
        if original.cleaned_track is None:
            db.forget_cleaned(str(path))
            return {"status": "skipped", "message": "there was no cleaned track to remove"}
        drop = self._ours(original, path, job_id)
        if not drop:
            raise RuntimeError(
                f"the track named “{settings.track_title}” in this file is not "
                f"one this install wrote, so it is left alone - remove it with "
                f"the tool that made it")

        before = path.stat().st_size
        work = Path(tempfile.mkdtemp(prefix="cleanarr-", dir=str(path.parent)))
        try:
            self._stage(job_id, "remuxing", "removing the cleaned track")
            candidate = work / f"remux{path.suffix}"
            dropped = media.remove_cleaned_track(
                original, candidate, drop=drop,
                progress=self._progress(job_id, "remuxing"))

            self._stage(job_id, "verifying", "checking the new file")
            media.verify_replacement(
                original, candidate, expected_audio=len(original.audio) - dropped,
                expect_cleaned=False)
            media.swap_in(candidate, path, keep_backup=settings.keep_backup)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        freed = max(0, before - path.stat().st_size)
        db.forget_cleaned(str(path))
        note = arr.refresh_for(settings, str(path))
        return {"status": "done", "muted": 0, "added_bytes": 0,
                "message": f"removed the cleaned track · {freed / 1e6:.0f} MB back · "
                           f"{note}"}

    # -- the work ---------------------------------------------------------
    def run(self, job_id: int, path: Path, force: bool = False, title: str = "",
            more_subtitles=None) -> dict:
        settings = self.settings
        if not path.exists():
            raise FileNotFoundError(_missing(path))

        self._stage(job_id, "probing", "reading the file")
        original = media.probe(path)
        looks_ours = [a for a in original.audio if a.is_cleaned]
        existing = self._ours(original, path, job_id)
        if looks_ours and not existing:
            # Every track that matches the name is one we cannot claim, so
            # cleaning would either add a confusing duplicate name or drop
            # somebody else's audio.
            raise RuntimeError(
                f"this file already has an audio track named "
                f"“{settings.track_title}”, and nothing in the file says "
                f"Cleanarr wrote it - pick a different name for the new track "
                f"in Settings, so an existing track is never replaced by mistake")
        if existing and not force:
            return {"status": "skipped",
                    "message": f"already has a “{settings.track_title}” track"}
        source = media.pick_source_track(original)
        db.update(job_id, duration=original.duration)

        # A remux needs room for a second copy of the file while it is built.
        needed = path.stat().st_size + 512 * 1024 * 1024
        if media.free_space(path) < needed:
            raise RuntimeError(
                f"not enough free space beside {path.name}: needs about "
                f"{needed / 1e9:.1f} GB")

        _sweep_stale_work(path.parent)
        work = Path(tempfile.mkdtemp(prefix="cleanarr-", dir=str(path.parent)))
        try:
            self._stage(job_id, "extracting", "pulling out the audio")
            subtitle_file = work / "subtitles.srt"
            wav = media.extract_for_asr(path, source, work / "asr.wav",
                                        subtitles=media.pick_subtitles(original),
                                        subtitle_dest=subtitle_file)

            self._stage(job_id, "listening", "listening for profanity")
            transcript = asr.transcribe(
                wav, model_name=settings.model, device=settings.device,
                compute_type=settings.compute_type,
                trim_silence=settings.trim_silence,
                # If the card is full when we get there, this is how we clear
                # it - Ollama's lease outlives any wait we could sit through.
                free_vram=lambda: judge.release_vram(settings.judge_url),
                # Set, and the listening happens on somebody else's Whisper
                # server instead - no model here, no GPU here.
                remote_url=(settings.asr_url if settings.asr_backend == "remote" else ""),
                remote_model=settings.asr_remote_model,
                remote_key=settings.asr_api_key,
                cache_dir=self.cache_dir,
                download_root=str(self.cache_dir / "models"),
                progress=self._progress(job_id, "listening"))

            self._stage(job_id, "matching", "checking the word list")
            context_words = {w.strip().lower() for w in settings.check_in_context
                             if w.strip()}
            # This show's own exceptions join the household's.
            here = (settings.allow_words_by_title or {}).get(title, [])
            matcher = words.Matcher(
                categories=tuple(settings.categories),
                never=frozenset(words.NEVER) | {w.lower() for w in settings.allow_words}
                | {w.lower() for w in here},
                extra=tuple(settings.custom_words),
                context_words=frozenset(context_words))
            matches = matcher.find(transcript.words)

            self._stage(job_id, "judging", "checking the ambiguous ones")
            # What the subtitles say at each word: evidence for whoever reviews
            # the job, and - only with the subtitle second opinion on - the
            # deciding word on the uncertain ones. The file's own track first,
            # else a subtitle file beside it.
            cues, found_where = [], ""
            source_file = (subtitle_file if subtitle_file.exists()
                           else media.find_sidecar_subtitles(path))
            if source_file is not None:
                cues = subtitles.parse_srt(source_file.read_text(encoding="utf8",
                                                                 errors="replace"))
            # Only when they would decide something: reading another copy of a
            # film means reading that whole file.
            if not cues and settings.subtitle_opinion and more_subtitles is not None:
                self._stage(job_id, "judging", "looking for subtitles elsewhere")
                text, found_where = more_subtitles(work)
                cues = subtitles.parse_srt(text) if text else []
                # Borrowed subtitles may be for a slightly different cut.
                cues, shift = subtitles.align(cues, transcript.words)
                if shift:
                    found_where += f", moved {abs(shift):g} s to line up"
            matches, note = subtitles.annotate(matches, cues)
            if note:
                print(f"[cleanarr] job {job_id}: {note}", flush=True)
            settled, kept = [], []
            if settings.subtitle_opinion and not note:
                settled, kept, matches = subtitles.decide(matches, context_words)
            # With the subtitle opinion on, say so when it had nothing to go
            # on - otherwise a job with no subtitles reads as though they were
            # used and simply agreed with everything.
            sub_note = ""
            if settings.subtitle_opinion:
                if not cues:
                    sub_note = " · no subtitles found to check against"
                elif note:
                    sub_note = " · subtitles set aside (another release?)"
                elif found_where:
                    sub_note = f" · subtitles from {found_where}"
            matches, judged = self._adjudicate(matches, transcript.words, settings)
            matches = sorted([*settled, *matches], key=lambda m: m.start)
            kept = sorted([*kept, *judged], key=lambda m: m.start)
            db.save_detections(job_id, matches, kept)
            spans = words.to_spans(matches, settings.pad_start, settings.pad_end)

            if not spans:
                return {"status": "skipped", "muted": 0,
                        "message": "nothing to mute - no profanity found" + sub_note,
                        "cached_transcript": transcript.cached}

            self._stage(job_id, "muting",
                        f"muting {len(matches)} word{'s' if len(matches) != 1 else ''}")
            candidate = work / f"remux{path.suffix}"
            media.build_cleaned_file(
                original, source, spans, candidate, title=settings.track_title,
                fade=settings.fade, drop_audio=existing,
                surround_bitrate=settings.bitrate_surround,
                stereo_bitrate=settings.bitrate_stereo,
                progress=self._progress(job_id, "muting"))

            self._stage(job_id, "verifying", "checking the new file")
            checked = media.verify_replacement(
                original, candidate,
                expected_audio=len(original.audio) + 1 - len(existing))
            new_track = checked.cleaned_track
            added = (media.track_bytes(candidate, new_track.audio_index)
                     if new_track else 0)
            media.swap_in(candidate, path, keep_backup=settings.keep_backup)
            note = arr.refresh_for(settings, str(path))
            return {"status": "done", "muted": len(matches), "added_bytes": added,
                    "message": f"added “{settings.track_title}” · "
                               f"{len(matches)} muted · {added / 1e6:.0f} MB · "
                               f"{note}{sub_note}",
                    "cached_transcript": transcript.cached}
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _sweep_stale_work(folder: Path, older_than: float = 6 * 3600) -> None:
    """Delete scratch directories a killed run left behind.

    The scratch directory sits beside the media file so the finished remux can
    be moved into place without crossing filesystems. That also means a
    container killed mid-job leaves several GB inside the user's TV folder,
    where it is both confusing and expensive.
    """
    now = time.time()
    for leftover in folder.glob("cleanarr-*"):
        try:
            if leftover.is_dir() and now - leftover.stat().st_mtime > older_than:
                shutil.rmtree(leftover, ignore_errors=True)
        except OSError:
            pass


def more_subtitles(settings, kind: str, source: str, source_id: str, path: Path,
                   work: Path, title: str = "") -> tuple[str, str]:
    """(SRT text, where it came from) for a file with no subtitles of its own,
    or ("", "").

    In order: what the media server holds for this very file (subtitles it
    downloaded, say); for a film, another copy of it that the server knows
    about - a WEB-DL beside a WEBRip often carries the subtitles the other
    lacks; and, with subtitle_search on, a search the server makes online.
    Any of these may be timed for a different cut: the caller lines them up,
    and sets them aside if they still disagree.
    """
    name = source if source in ("plex", "jellyfin") else getattr(settings, "media_server", "")
    client = library.server(settings, name) if name in ("plex", "jellyfin") else None
    if client is None:
        return "", ""
    here = str(path)
    try:
        item_id = (str(source_id) if source == name and source_id
                   else client.find(kind, here, title))
        versions = client.versions(item_id) if item_id else []
    except Exception as exc:  # noqa: BLE001
        print(f"[cleanarr] subtitles from {name}: {exc}", flush=True)
        return "", ""

    def fetch(v) -> tuple[str, str]:
        for label, url in v["subtitles"]:
            try:
                text = client.download(url)
            except Exception:  # noqa: BLE001
                continue
            if subtitles.parse_srt(text):
                return text, f"{name.title()} ({label})"
        return "", ""

    for v in (v for v in versions if v["path"] == here):
        found = fetch(v)
        if found[0]:
            return found
    if kind == "movie":
        for v in (v for v in versions if v["path"] != here):
            other = Path(v["path"])
            if not other.is_file():
                continue
            where = f"another copy ({other.name})"
            try:
                stream = media.pick_subtitles(media.probe(other))
            except Exception:  # noqa: BLE001
                stream = None
            dest = work / "other-copy.srt"
            if stream is not None and media.extract_subtitles(other, stream, dest):
                text = dest.read_text(encoding="utf8", errors="replace")
                if subtitles.parse_srt(text):
                    return text, where
            side = media.find_sidecar_subtitles(other)
            if side is not None:
                text = side.read_text(encoding="utf8", errors="replace")
                if subtitles.parse_srt(text):
                    return text, where
            text, _label = fetch(v)
            if text:
                return text, where

    if not getattr(settings, "subtitle_search", False) or not item_id:
        return "", ""
    try:
        attached = client.search_subtitles(item_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[cleanarr] {name} subtitle search: {exc}", flush=True)
        return "", ""
    if not attached:
        return "", ""
    # The server fetches the file after answering, so it can take a moment to
    # appear on the item.
    for attempt in range(SEARCH_POLLS):
        try:
            mine = [v for v in client.versions(item_id) if v["path"] == here]
        except Exception:  # noqa: BLE001
            mine = []
        for v in mine:
            text, _label = fetch(v)
            if text:
                return text, f"a {name.title()} search ({attached})"
        if attempt + 1 < SEARCH_POLLS:
            time.sleep(SEARCH_WAIT)
    print(f"[cleanarr] {name} attached {attached!r} but it never appeared on the file",
          flush=True)
    return "", ""


# How long to wait for subtitles the media server was asked to fetch.
SEARCH_POLLS = 6
SEARCH_WAIT = 3.0


def run_job(job_id: int, settings: config.Settings, cache_dir: Path,
            should_cancel=None) -> None:
    """Run one queued job and record what happened to it."""
    job = db.get(job_id)
    if job is None:
        return
    # The library's path is opened as given: this container has the media
    # mounted where the library says it is, or the job fails saying so.
    path = Path(job["path"])
    # What the added track is called, and what it has been called before, so
    # detection and the name written agree with the settings.
    media.set_clean_title(settings.track_title, settings.known_track_titles)
    # Removals run ffmpeg too, so the thread cap applies to both.
    media.THREADS = settings.ffmpeg_threads
    db.update(job_id, status="running", started_at=time.time(), message="",
              stage="probing", progress=0.0)
    try:
        pipeline = Pipeline(settings, cache_dir, should_cancel)
        action = (job["action"] if "action" in job.keys() else "clean") or "clean"
        if action == "remove":
            result = pipeline.remove(job_id, path)
        else:
            keys = job.keys()
            result = pipeline.run(
                job_id, path, force=bool(job["force"]), title=job["title"],
                more_subtitles=lambda work: more_subtitles(
                    settings, job["kind"] if "kind" in keys else "",
                    job["source"] if "source" in keys else "",
                    job["source_id"] if "source_id" in keys else "", path, work,
                    title=job["title"]))
        db.update(job_id, status=result.get("status", "done"),
                  muted=result.get("muted", 0), message=result.get("message", ""),
                  added_bytes=result.get("added_bytes", 0),
                  progress=1.0, stage="", finished_at=time.time())
    except Cancelled:
        db.update(job_id, status="cancelled", message="cancelled", stage="",
                  finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        db.update(job_id, status="failed", message=f"{type(exc).__name__}: {exc}"[:500],
                  stage="", finished_at=time.time())
