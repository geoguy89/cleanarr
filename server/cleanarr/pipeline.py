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
from pathlib import Path

from . import arr, asr, config, db, judge, media, words

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
        # code's: an empty setting means the built-in list, anything else
        # replaces it. A word taken off the list is simply muted.
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
                leave.append(words.Match(
                    start=match.start, end=match.end, text=match.text,
                    category=match.category, needs_review=True,
                    reason=("left in: reverent, not an exclamation"
                            if heard.lower().startswith("reverent")
                            else f"left in: heard as “{heard}” here")))
            else:
                if i in asked and verdicts:
                    match = words.Match(
                        start=match.start, end=match.end, text=match.text,
                        category=match.category, needs_review=True,
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
                original, candidate, expected_audio=len(original.audio) - dropped)
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
    def run(self, job_id: int, path: Path, force: bool = False) -> dict:
        settings = self.settings
        media.THREADS = settings.ffmpeg_threads
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
            wav = media.extract_for_asr(path, source, work / "asr.wav")

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
            matcher = words.Matcher(
                categories=tuple(settings.categories),
                never=frozenset(words.NEVER) | {w.lower() for w in settings.allow_words},
                extra=tuple(settings.custom_words),
                context_words=frozenset(context_words))
            matches = matcher.find(transcript.words)

            self._stage(job_id, "judging", "checking the ambiguous ones")
            matches, kept = self._adjudicate(matches, transcript.words, settings)
            db.save_detections(job_id, matches, kept)
            spans = words.to_spans(matches, settings.pad_start, settings.pad_end)

            if not spans:
                return {"status": "skipped", "muted": 0,
                        "message": "nothing to mute - no profanity found",
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
                               f"{note}",
                    "cached_transcript": transcript.cached}
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _audio_ext(container: str) -> str:
    return "mka" if container in ("mkv", "mka") else "m4a"


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
    db.update(job_id, status="running", started_at=time.time(), message="",
              stage="probing", progress=0.0)
    try:
        pipeline = Pipeline(settings, cache_dir, should_cancel)
        action = (job["action"] if "action" in job.keys() else "clean") or "clean"
        if action == "remove":
            result = pipeline.remove(job_id, path)
        else:
            result = pipeline.run(job_id, path, force=bool(job["force"]))
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
