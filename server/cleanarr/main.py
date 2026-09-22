"""The HTTP side: a small API and the page that drives it."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import arr, auth, config, db, judge, library, media, words
from .worker import Worker

WEB_DIR = Path(os.environ.get("CLEANARR_WEB", "/app/web"))
CACHE_DIR = Path(os.environ.get("CLEANARR_CACHE", "/config/cache"))

app = FastAPI(title="Cleanarr", docs_url="/api/docs", redoc_url=None)
worker = Worker(CACHE_DIR)

# For the pages that ask Sonarr and Radarr two slow questions at once. Both
# are the remote service waiting on its own database, so they overlap cleanly.
POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cleanarr-arr")


@app.on_event("startup")
def _startup() -> None:
    db.connect()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    resumed = db.requeue_interrupted()
    if resumed:
        print(f"[cleanarr] re-queued {resumed} job(s) interrupted by a restart",
              flush=True)
    worker.start()
    # Learn the import dates now, so the first person to sort by "recently
    # added" is not the one who pays for them.
    settings = config.load()
    _import_dates(arr.Sonarr(settings.sonarr.url, settings.sonarr.api_key))


@app.on_event("shutdown")
def _shutdown() -> None:
    worker.stop()



# ---------------------------------------------------------------------------
#  Who is allowed in
# ---------------------------------------------------------------------------

@app.middleware("http")
async def _require_login(request: Request, call_next):
    """Refuse anything that is not open, unless the cookie checks out.

    A middleware rather than a dependency on each route, because the failure
    mode of the per-route version is a route somebody forgets to decorate -
    and an endpoint that quietly needs no password is worse than no password
    at all, since you believe you have one.
    """
    settings = config.load()
    if not settings.auth_user or auth.is_open(request.url.path):
        return await call_next(request)

    token = request.cookies.get(auth.COOKIE, "")
    if not auth.verify(token, settings.auth_secret, settings.auth_user):
        return JSONResponse({"error": "not signed in"}, status_code=401)
    return await call_next(request)


@app.get("/api/auth/state")
def auth_state():
    """What the page needs before it knows whether to show a login form."""
    settings = config.load()
    return {
        "configured": bool(settings.auth_user),
        "username": settings.auth_user,
    }


@app.post("/api/auth/setup")
def auth_setup(payload: dict, request: Request, response: Response):
    """Create the account. Only possible while there isn't one.

    Once a username exists this refuses, so the setup form cannot be used to
    take over an instance - changing the password afterwards needs the old one.
    """
    settings = config.load()
    if settings.auth_user:
        raise HTTPException(409, "a login is already set up")

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if len(username) < 3:
        raise HTTPException(400, "the username needs at least 3 characters")
    if len(password) < 8:
        raise HTTPException(400, "the password needs at least 8 characters")

    settings.auth_hash, settings.auth_salt = auth.hash_password(password)
    settings.auth_secret = auth.new_secret()
    settings.auth_user = username
    config.save(settings)
    _set_session(response, auth.issue(username, settings.auth_secret), request)
    return {"ok": True, "username": username}


@app.post("/api/auth/login")
def auth_login(payload: dict, request: Request, response: Response):
    settings = config.load()
    if not settings.auth_user:
        return {"ok": True, "no_login_required": True}

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    # One message for both failures. "No such user" tells anyone guessing
    # which half they got right.
    if (username != settings.auth_user
            or not auth.check_password(password, settings.auth_hash, settings.auth_salt)):
        raise HTTPException(401, "wrong username or password")

    _set_session(response, auth.issue(username, settings.auth_secret), request)
    return {"ok": True, "username": username}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.post("/api/auth/change")
def auth_change(payload: dict, request: Request, response: Response):
    """Change the password, or turn the login off entirely.

    The current password is required either way, including to disable - a
    cookie someone left open on a laptop should not be enough to remove the
    lock from the whole instance.
    """
    settings = config.load()
    if not settings.auth_user:
        raise HTTPException(409, "there is no login to change")
    if not auth.check_password(str(payload.get("current", "")),
                               settings.auth_hash, settings.auth_salt):
        raise HTTPException(401, "that is not the current password")

    if payload.get("disable"):
        settings.auth_user = settings.auth_hash = ""
        settings.auth_salt = settings.auth_secret = ""
        config.save(settings)
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True, "disabled": True}

    password = str(payload.get("password", ""))
    if len(password) < 8:
        raise HTTPException(400, "the password needs at least 8 characters")
    username = str(payload.get("username", "")).strip() or settings.auth_user
    settings.auth_hash, settings.auth_salt = auth.hash_password(password)
    # A new signing key as well, so a password change ends every other session
    # - which is the whole reason most people change one.
    settings.auth_secret = auth.new_secret()
    settings.auth_user = username
    config.save(settings)
    _set_session(response, auth.issue(username, settings.auth_secret), request)
    return {"ok": True, "username": username}


def _set_session(response: Response, token: str, request: Request) -> None:
    # `secure` only when the request arrived over HTTPS: setting it always
    # would mean the cookie is silently dropped on a plain LAN address, which
    # is how most of these run, and the login would appear to do nothing.
    response.set_cookie(
        auth.COOKIE, token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


# ---------------------------------------------------------------------------
#  The speech model
# ---------------------------------------------------------------------------
#  It is NOT in the container image - it is downloaded from Hugging Face the
#  first time it is needed and kept in /config/cache/models, so it survives
#  updates. Left to itself that download happens silently in the middle of the
#  first job, which looks like the job has hung. These endpoints let the page
#  say what is on disk and fetch one on purpose.

#  One model is offered, and that is a decision rather than a limitation.
#  small.en is faster and misses more; large-v3 is slower and - measured on a
#  real episode - caught FEWER swears, because it favours tidy prose and tidy
#  prose smooths swearing away. Offering three choices where two are worse
#  invites people to pick a worse one. Anyone who disagrees can still set
#  `model:` by hand in config.yaml; it is the settings page that has an
#  opinion, not the code.
BUILTIN_MODEL = "medium.en"
MODEL_SIZES = {"medium.en": "1.5 GB"}

_downloading: dict[str, str] = {}


def _model_dir(name: str) -> Path:
    return CACHE_DIR / "models" / f"models--Systran--faster-whisper-{name}"


def _on_disk(name: str) -> int:
    """Bytes the model occupies, or 0 if it has not been fetched."""
    folder = _model_dir(name)
    if not folder.exists():
        return 0
    # Symlinks are skipped, not followed. A Hugging Face cache stores each file
    # once under blobs/ and links to it from snapshots/, so counting both
    # reports every model at twice its real size.
    return sum(f.stat().st_size for f in folder.rglob("*")
               if f.is_file() and not f.is_symlink())


@app.get("/api/models")
def list_models():
    settings = config.load()
    offered = [BUILTIN_MODEL]
    return {
        "selected": settings.model,
        "folder": str(CACHE_DIR / "models"),
        "items": [{
            "name": name,
            "approx_size": MODEL_SIZES.get(name, ""),
            "bytes": _on_disk(name),
            "ready": _on_disk(name) > 0,
            "downloading": _downloading.get(name, ""),
        } for name in offered],
    }


@app.post("/api/models/{name}/download")
def download_model(name: str):
    """Fetch a model now, rather than during the first job."""
    if name in _downloading:
        return {"started": False, "already": True}
    if _on_disk(name):
        return {"started": False, "ready": True}

    def fetch() -> None:
        _downloading[name] = "downloading"
        try:
            from faster_whisper.utils import download_model as pull
            pull(name, cache_dir=str(CACHE_DIR / "models"))
            print(f"[cleanarr] downloaded speech model {name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanarr] could not download {name}: {exc}", flush=True)
        finally:
            _downloading.pop(name, None)

    threading.Thread(target=fetch, daemon=True).start()
    return {"started": True}


# ---------------------------------------------------------------------------
#  The second opinion's model
# ---------------------------------------------------------------------------

@app.get("/api/asr/remote-models")
def remote_models(url: str = ""):
    """What the Whisper server offers, for the dropdown.

    Model names are the server's own - one instance calls it
    "Systran/faster-whisper-medium.en", another "whisper-1", another something
    it made up. Asking beats guessing. A server with no /v1/models is not a
    fault; the field stays free text in that case.
    """
    settings = config.load()
    base = (url or settings.asr_url).strip().rstrip("/")
    if not base:
        raise HTTPException(400, "no Whisper server address set")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        resp = httpx.get(f"{base}/v1/models", timeout=15.0)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "models": []}

    # OpenAI shape is {"data": [{"id": ...}]}; some servers return a bare list.
    rows = body.get("data") if isinstance(body, dict) else body
    names = [str(r.get("id") or r.get("name") or "") for r in (rows or [])
             if isinstance(r, dict)]
    return {"ok": True, "models": sorted(n for n in names if n)}


@app.post("/api/asr/test")
def test_remote_asr(payload: dict | None = None):
    """Is the Whisper server there, and does it give word timings?

    The second half is the part that matters. A server that accepts the request
    and returns only segment timings would make every mute four seconds long
    instead of half a second, and it would do it silently - so this is checked
    up front rather than discovered on a cleaned episode.
    """
    from . import asr as _asr
    settings = config.load()
    url = str((payload or {}).get("url") or settings.asr_url)
    model = str((payload or {}).get("model") or settings.asr_remote_model)
    if not url:
        raise HTTPException(400, "no Whisper server address set")
    return _asr.probe_remote(url, model, settings.asr_api_key)


@app.get("/api/judge/models")
def judge_models():
    """What the Ollama instance already has, so the page can say."""
    settings = config.load()
    if not settings.judge_url:
        return {"reachable": False, "models": [], "has_selected": False}
    try:
        resp = httpx.get(f"{settings.judge_url.rstrip('/')}/api/tags", timeout=10.0)
        resp.raise_for_status()
        names = [m.get("name", "") for m in (resp.json().get("models") or [])]
    except (httpx.HTTPError, ValueError) as exc:
        return {"reachable": False, "error": str(exc), "models": [],
                "has_selected": False}
    # Ollama reports "qwen3.5:9b"; a model asked for without a tag is ":latest".
    wanted = settings.judge_model
    has = any(n == wanted or n.split(":")[0] == wanted.split(":")[0] for n in names)
    return {"reachable": True, "models": names, "has_selected": has,
            "selected": wanted}


@app.post("/api/judge/pull")
def judge_pull():
    """Tell Ollama to fetch the model we are configured to ask.

    Ollama would pull it on the first question anyway, but that happens inside
    a job, takes several minutes, and looks like the job has stalled. Doing it
    on purpose from the settings page is the same download with somebody
    watching it.
    """
    settings = config.load()
    if not settings.judge_url or not settings.judge_model:
        raise HTTPException(400, "set an Ollama address and model first")
    if _downloading.get("__ollama__"):
        return {"started": False, "already": True}

    def fetch() -> None:
        _downloading["__ollama__"] = settings.judge_model
        try:
            # Streamed, so Ollama does not time the request out on a long
            # download; the body is drained and discarded.
            with httpx.stream("POST", f"{settings.judge_url.rstrip('/')}/api/pull",
                              json={"model": settings.judge_model},
                              timeout=None) as resp:
                for _ in resp.iter_lines():
                    pass
            print(f"[cleanarr] Ollama pulled {settings.judge_model}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanarr] Ollama pull failed: {exc}", flush=True)
        finally:
            _downloading.pop("__ollama__", None)

    threading.Thread(target=fetch, daemon=True).start()
    return {"started": True, "model": settings.judge_model}


@app.get("/api/judge/pull")
def judge_pull_state():
    return {"downloading": _downloading.get("__ollama__", "")}


# ---------------------------------------------------------------------------
#  Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    settings = config.load()
    data = settings.public()
    data["available_categories"] = [
        {"key": k, "label": words.CATEGORY_LABELS[k]} for k in words.CATEGORIES]
    data["hold_policies"] = arr.hold_policies(settings.media_server)
    return data


@app.put("/api/settings")
def put_settings(payload: dict):
    settings = config.load()
    for section in ("sonarr", "radarr"):
        if section in payload and isinstance(payload[section], dict):
            current = getattr(settings, section)
            for key, value in payload[section].items():
                # A masked key means "leave it alone", so the UI can save the
                # form without ever holding the real key.
                if key == "api_key" and set(str(value)) == {"*"}:
                    continue
                setattr(current, key, value)
    for key in ("categories", "custom_words", "allow_words", "pad_start", "pad_end",
                "fade", "model", "device", "compute_type", "trim_silence", "keep_backup",
                "asr_backend", "asr_url", "asr_remote_model",
                "track_title", "path_map", "plex_url", "judge_url", "judge_model",
                "media_server", "jellyfin_url", "library_source",
                "judge_threads", "judge_keep_alive", "bitrate_surround",
                "bitrate_stereo", "ffmpeg_threads", "hold_policy",
                "check_in_context"):
        if key in payload:
            setattr(settings, key, payload[key])
    if "plex_token" in payload and set(str(payload["plex_token"])) != {"*"}:
        settings.plex_token = payload["plex_token"]
    # Same masking rule as the other secrets: all-stars means "leave it".
    if "asr_api_key" in payload and set(str(payload["asr_api_key"])) != {"*"}:
        settings.asr_api_key = payload["asr_api_key"]
    if ("jellyfin_api_key" in payload
            and set(str(payload["jellyfin_api_key"])) != {"*"}):
        settings.jellyfin_api_key = payload["jellyfin_api_key"]
    config.save(settings)
    return config.load().public()


@app.post("/api/settings/test/{service}")
def test_service(service: str):
    settings = config.load()
    try:
        if service == "sonarr":
            return arr.Sonarr(settings.sonarr.url, settings.sonarr.api_key).test()
        if service == "radarr":
            return arr.Radarr(settings.radarr.url, settings.radarr.api_key).test()
        if service == "plex":
            return library.PlexLibrary(settings.plex_url, settings.plex_token).test()
        if service == "jellyfin":
            return library.JellyfinLibrary(settings.jellyfin_url,
                                           settings.jellyfin_api_key).test()
    except library.LibraryError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)
    except arr.ArrError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)
    raise HTTPException(404, "no such service")


# ---------------------------------------------------------------------------
#  Browsing what you already own
# ---------------------------------------------------------------------------

#  When each show last got a file
#  -----------------------------
#  Two pages of Sonarr history cost about fourteen seconds against this
#  library, and all they produce is a sort key. So they are fetched alongside
#  the series list rather than after it, and the answer is kept for a while:
#  a date that is ten minutes stale cannot change which show is at the top.

#  Stale-while-revalidate: whatever was learned last time is handed back at
#  once and a refresh runs behind the page. Nobody waits fourteen seconds for
#  a sort key, and the only cost of being a few minutes out of date is that a
#  show which arrived in those minutes sorts one place too low.

_IMPORT_DATES: dict[str, object] = {"at": 0.0, "value": {}, "running": False}
_IMPORT_TTL = 600.0
_IMPORT_LOCK = threading.Lock()


def _refresh_import_dates(client: arr.Sonarr) -> None:
    try:
        fresh = client.latest_import_dates()
        _IMPORT_DATES.update(at=time.time(), value=fresh)
    except arr.ArrError:
        pass                    # keep what we had; this is only a sort key
    finally:
        with _IMPORT_LOCK:
            _IMPORT_DATES["running"] = False


def _import_dates(client: arr.Sonarr) -> dict[int, str]:
    with _IMPORT_LOCK:
        stale = time.time() - float(_IMPORT_DATES["at"]) >= _IMPORT_TTL
        if stale and not _IMPORT_DATES["running"]:
            _IMPORT_DATES["running"] = True
            POOL.submit(_refresh_import_dates, client)
    return _IMPORT_DATES["value"]               # type: ignore[return-value]


@app.get("/api/series")
def series():
    settings = config.load()
    source = library.build(settings)
    # Only Sonarr keeps an import history to sort by; a media server carries
    # the date on the item itself, which `added` already holds.
    landed = (_import_dates(source.sonarr)
              if getattr(source, "name", "") == "arr" else {})
    try:
        items = source.shows()
    except (arr.ArrError, library.LibraryError) as exc:
        raise HTTPException(502, str(exc))

    # Mark up each show from the job history, matching on the show's folder.
    # One pass over the jobs beats one query per show for a library this size.
    known = db.job_paths()
    watched = {row["source_id"] for row in db.monitors("sonarr")}
    for show in items:
        # Plex does not report a show's folder, so fall back to matching the
        # episodes this show actually has once they have been listed. Until
        # then a show simply shows no counts rather than wrong ones.
        folder = (show.get("path") or "").rstrip("/") + "/"
        statuses = [s for p, s in known.items() if folder != "/" and p.startswith(folder)]
        show["cleaned"] = sum(1 for s in statuses if s in ("done", "skipped"))
        show["pending"] = sum(1 for s in statuses if s in ("queued", "running"))
        show["failed"] = sum(1 for s in statuses if s == "failed")
        show["monitored"] = str(show["id"]) in watched
        # When something last arrived for this show. Falls back to the day the
        # show itself was added, which is all Sonarr offers for a back catalogue
        # imported before its history was being kept - and is what a media
        # server reports directly.
        show["latest"] = landed.get(show["id"]) or show.get("added", "")

    source = _poster_source(settings, "show")
    for show in items:
        show["source"] = source
    threading.Thread(target=_warm_posters,
                     args=(source, [s["id"] for s in items]),
                     daemon=True).start()
    return {"items": items}


@app.get("/api/home")
def home(limit: int = 12):
    """What arrived lately, on both sides, plus how the queue is doing.

    One request rather than three, because this is the first page the app
    opens and it should not take three round trips to draw.
    """
    settings = config.load()
    episodes: list[dict] = []
    films: list[dict] = []
    problems: list[str] = []

    # Asked at the same time: neither answer depends on the other, and this is
    # the page that opens first.
    source = library.build(settings)
    shows_label = "Sonarr" if source.name == "arr" else source.name.title()
    films_label = "Radarr" if source.name == "arr" else source.name.title()

    recent = POOL.submit(source.recent_episodes, limit)
    all_films = POOL.submit(source.movies)
    try:
        episodes = recent.result()
    except (arr.ArrError, library.LibraryError) as exc:
        problems.append(f"{shows_label}: {exc}")
    try:
        films = sorted((m for m in all_films.result() if m.get("added")),
                       key=lambda m: m["added"], reverse=True)[:limit]
    except (arr.ArrError, library.LibraryError) as exc:
        problems.append(f"{films_label}: {exc}")

    # One lookup for both lists: a poster wall of "already cleaned" badges is
    # the whole point of showing them here.
    known = db.cleaned_paths([e["path"] for e in episodes]
                             + [m["path"] for m in films])
    watched = {(r["source"], r["source_id"]) for r in db.monitors()}
    for item in episodes:
        item["monitored"] = ("sonarr", str(item["series_id"])) in watched
    problems = [p for p in problems if p]
    for group, kind in ((episodes, "show"), (films, "movie")):
        for item in group:
            job = known.get(item["path"]) or {}
            item["source"] = _poster_source(settings, kind)
            item["job_status"] = job.get("status", "")
            item["muted"] = job.get("muted")
            item["cleaned_at"] = job.get("finished_at")

    counts = db.count_by_status()
    return {
        "episodes": episodes, "movies": films, "problems": problems,
        "stats": db.stats(), "monitors": len(db.monitors()),
        "queued": counts.get("queued", 0), "running": counts.get("running", 0),
        "failed": counts.get("failed", 0),
        "busy": worker.busy, "holding": worker.holding,
    }


@app.get("/api/calendar")
def calendar(days: int = 21):
    """What is due to air, so there is no need to go over to Sonarr to look.

    Each row also says whether we are already set to clean the show, and that
    is the point of having it here rather than in Sonarr: seeing that
    something starts on Friday is the moment you decide it should arrive clean.
    """
    settings = config.load()
    # Only Sonarr knows what has not aired yet - a media server can only see
    # what it already has - so this is empty rather than wrong for the others.
    if settings.library_source != "arr":
        return {"items": [], "days": days, "unavailable": True}
    try:
        items = arr.Sonarr(settings.sonarr.url,
                           settings.sonarr.api_key).calendar(max(1, min(days, 90)))
    except arr.ArrError as exc:
        raise HTTPException(502, str(exc))
    watched = {r["source_id"] for r in db.monitors("sonarr")}
    for episode in items:
        episode["monitored"] = str(episode["series_id"]) in watched
    return {"items": items, "days": days}


@app.get("/api/series/{series_id}/episodes")
def episodes(series_id: str):
    settings = config.load()
    try:
        items = library.build(settings).episodes(series_id)
    except (arr.ArrError, library.LibraryError) as exc:
        raise HTTPException(502, str(exc))
    known = db.cleaned_paths([e["path"] for e in items])
    for episode in items:
        job = known.get(episode["path"]) or {}
        episode["job_status"] = job.get("status", "")
        episode["job_id"] = job.get("job_id")
        episode["muted"] = job.get("muted")
        episode["cleaned_at"] = job.get("finished_at")
        episode["added_bytes"] = job.get("added_bytes")
    return {"items": items}


@app.get("/api/movies")
def movies():
    settings = config.load()
    try:
        items = library.build(settings).movies()
    except (arr.ArrError, library.LibraryError) as exc:
        raise HTTPException(502, str(exc))
    known = db.cleaned_paths([m["path"] for m in items])
    for movie in items:
        job = known.get(movie["path"]) or {}
        movie["job_status"] = job.get("status", "")
        movie["job_id"] = job.get("job_id")
        movie["muted"] = job.get("muted")
        movie["cleaned_at"] = job.get("finished_at")
        movie["latest"] = movie.get("added", "")

    source = _poster_source(settings, "movie")
    for movie in items:
        movie["source"] = source
    threading.Thread(target=_warm_posters,
                     args=(source, [m["id"] for m in items]),
                     daemon=True).start()
    return {"items": items}


#  Artwork
#  -------
#  Proxied rather than linked for two reasons: the images need an API key,
#  which has no business in a browser, and the remote ones point at TVDB,
#  which makes a library sitting on the disk depend on the internet to look
#  like anything.
#
#  A thousand shows means a thousand images the first time the page opens, so
#  fetches are capped and the rest are warmed in the background. After the
#  first pass every poster is served from disk.

POSTER_FETCHES = threading.Semaphore(4)
_warming: set[str] = set()
_warm_lock = threading.Lock()


def _poster_path(source: str, item_id: int | str) -> Path:
    folder = CACHE_DIR / "posters"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{source}-{item_id}.jpg"


def _poster_source(settings, kind: str) -> str:
    """Which service to ask for artwork, and the cache namespace it uses.

    Namespaced so switching library source does not serve one service's
    artwork under another's ids - they number their items differently.
    """
    if settings.library_source in ("plex", "jellyfin"):
        return settings.library_source
    return "sonarr" if kind == "show" else "radarr"


def _fetch_poster(source: str, item_id: int | str, settings) -> bool:
    cached = _poster_path(source, item_id)
    if cached.exists() and cached.stat().st_size > 0:
        return True

    # A media server serves its own artwork, and has it for everything it
    # knows about - including files Sonarr never imported.
    if source in ("plex", "jellyfin"):
        try:
            data = library.build(settings).poster(str(item_id))
        except Exception:  # noqa: BLE001
            return False
        if not data:
            return False
        tmp = cached.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(cached)
        return True

    arr_config = settings.sonarr if source == "sonarr" else settings.radarr
    if not arr_config.url:
        return False
    # Both Sonarr and Radarr serve artwork at /api/v3/mediacover/<id>/poster.jpg
    # - with no "series" or "movie" segment, which 404s.
    url = f"{arr_config.url.rstrip('/')}/api/v3/mediacover/{item_id}/poster.jpg"
    try:
        with POSTER_FETCHES:
            resp = httpx.get(url, headers={"X-Api-Key": arr_config.api_key},
                             timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
        if not resp.content:
            return False
        tmp = cached.with_suffix(".part")
        tmp.write_bytes(resp.content)
        tmp.replace(cached)
        return True
    except Exception:  # noqa: BLE001
        return False


def _warm_posters(source: str, ids: list[int]) -> None:
    """Fill the poster cache quietly, one library at a time."""
    with _warm_lock:
        if source in _warming:
            return
        _warming.add(source)
    try:
        for item_id in ids:
            if not _poster_path(source, item_id).exists():
                _fetch_poster(source, item_id, config.load())
    finally:
        with _warm_lock:
            _warming.discard(source)


@app.get("/api/poster")
def poster(source: str, id: str):
    # String, not int: Jellyfin item ids are 32-character hex.
    if not _fetch_poster(source, id, config.load()):
        raise HTTPException(404, "no poster")
    return FileResponse(str(_poster_path(source, id)), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/monitors")
def list_monitors():
    return {"items": [dict(r) for r in db.monitors()]}


@app.post("/api/monitors")
def add_monitor(payload: dict):
    source = str(payload.get("source", "sonarr"))
    source_id = str(payload.get("source_id", ""))
    if not source_id:
        raise HTTPException(400, "which show?")
    mode = "new_only" if payload.get("mode") == "new_only" else "catch_up"
    db.monitor_add(source, source_id, str(payload.get("title", "")), mode)
    return {"monitored": True, "source": source, "source_id": source_id, "mode": mode}


@app.delete("/api/monitors/{source}/{source_id}")
def drop_monitor(source: str, source_id: str):
    db.monitor_remove(source, source_id)
    return {"monitored": False}


@app.post("/api/monitors/check")
def check_monitors():
    """Look for new episodes now rather than at the next ten-minute sweep."""
    queued = worker.check_monitored()
    worker.nudge()
    return {"queued": queued, "checked_at": worker.last_monitor_check}


@app.post("/api/webhook/sonarr")
async def sonarr_webhook(request: Request):
    """Optional: Sonarr tells us the moment an episode is imported.

    Without this, a new episode waits up to ten minutes for the next sweep,
    which is fine. With it, cleaning starts as the download finishes. Point a
    Sonarr "Connect -> Webhook" at this URL with the On Import trigger.

    It answers immediately and does the work on a thread: Sonarr times its
    webhooks out and retries, and a slow reply would have it asking twice.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    event = str(payload.get("eventType", ""))
    if event in ("Test", "test"):
        return {"ok": True, "message": "Cleanarr heard you"}
    threading.Thread(target=_webhook_check, daemon=True).start()
    return {"ok": True, "event": event}


def _webhook_check() -> None:
    try:
        worker.check_monitored()
        worker.nudge()
    except Exception as exc:  # noqa: BLE001
        print(f"[cleanarr] webhook check failed: {exc}", flush=True)


# ---------------------------------------------------------------------------
#  Path mapping
#
#  The library reports paths as IT sees them. Those work unchanged when the
#  source and this container mount the same media at the same place; a media
#  server on another host, or one reaching storage over its own mount, reports
#  paths that mean nothing here. The symptom is every job failing with "not
#  found from this container" while the library browses perfectly, so the
#  mapping is visible, checkable and guessable rather than hand-edited.
# ---------------------------------------------------------------------------

def _visible_roots() -> list[str]:
    """Top-level directories this container can actually see.

    Excludes the ones every container has, so what is left is the media mounts.
    """
    skip = {"proc", "sys", "dev", "etc", "bin", "sbin", "lib", "lib64", "usr",
            "var", "run", "tmp", "root", "home", "opt", "boot", "srv", "media"}
    out = []
    for child in sorted(Path("/").iterdir()):
        if child.name in skip or not child.is_dir():
            continue
        out.append("/" + child.name)
    return out


def _suggest(reported: str, visible: list[str]) -> tuple[str, str]:
    """Find where a reported path lives here, and say which part matched.

    Returns (the ancestor of the reported path that was located, where it is
    locally), or ("", "").

    Searched two ways. Walking up the reported path handles a file this
    container has never seen, where ".../media/tv" still resolves. Walking in
    from the left of what remains handles a mount at a different depth:
    "/mnt/tank/media/tv" under a mounted /data is /data/media/tv.

    Deepest ancestor first, so the rule is the most specific one that resolves
    rather than a shallow coincidence.
    """
    parts = [p for p in reported.strip("/").split("/") if p]
    for end in range(len(parts), 0, -1):
        prefix = parts[:end]
        for root in visible:
            for start in range(len(prefix)):
                candidate = Path(root, *prefix[start:])
                try:
                    if candidate.is_dir():
                        return "/" + "/".join(prefix), str(candidate)
                except OSError:
                    continue
    return "", ""


@app.get("/api/paths")
def path_report():
    """Where the library says the media is, and whether we can reach it."""
    settings = config.load()
    visible = _visible_roots()
    try:
        roots = library.build(settings).roots()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "visible": visible, "roots": [],
                "path_map": settings.path_map or {}}

    out = []
    for r in roots:
        reported = r.get("path", "")
        mapped = config.map_path(reported, settings.path_map)
        ok = Path(mapped).is_dir()
        row = {**r, "mapped": mapped, "ok": ok, "suggestion": ""}
        if not ok:
            matched, found = _suggest(reported, visible)
            if found:
                row["suggestion"] = found
                # The rule is the part that differs, not the whole path, so one
                # rule covers every show under that root - and it is built from
                # the ancestor that actually matched, not the full path.
                row["rule"] = _common_rule(matched, found)
        out.append(row)
    return {"roots": out, "visible": visible,
            "path_map": settings.path_map or {},
            "source": getattr(settings, "library_source", "arr")}


def _common_rule(reported: str, found: str) -> dict:
    """Trim the matching tail off both sides to get the shortest rule."""
    a, b = reported.rstrip("/").split("/"), found.rstrip("/").split("/")
    while a and b and a[-1] == b[-1]:
        a.pop()
        b.pop()
    return {"from": "/".join(a) or "/", "to": "/".join(b) or "/"}


@app.post("/api/paths/check")
def path_check(payload: dict):
    """Resolve one path the way a job would, and say what happened."""
    settings = config.load()
    reported = (payload.get("path") or "").strip()
    if not reported:
        raise HTTPException(400, "no path given")
    mapping = payload.get("path_map")
    if mapping is None:
        mapping = settings.path_map
    mapped = config.map_path(reported, mapping)
    target = Path(mapped)
    if target.exists():
        return {"ok": True, "mapped": mapped,
                "kind": "directory" if target.is_dir() else "file",
                "message": f"Found it at {mapped}"}
    # How far up the tree it did get: "the mount is there but the show is not"
    # is a different problem from "nothing is mounted here".
    deepest = ""
    probe = target
    while probe != probe.parent:
        probe = probe.parent
        if probe.exists():
            deepest = str(probe)
            break
    matched, suggestion = _suggest(reported, _visible_roots())
    return {"ok": False, "mapped": mapped, "deepest_existing": deepest,
            "suggestion": suggestion,
            "rule": _common_rule(matched, suggestion) if suggestion else None,
            "message": (f"{mapped} is not there. This container can see "
                        f"as far as {deepest or '/'}.")}


@app.get("/api/file")
def file_info(path: str):
    """What is in one file: used to show existing tracks before queueing."""
    settings = config.load()
    real = Path(config.map_path(path, settings.path_map))
    if not real.exists():
        raise HTTPException(404, f"{real} not found from this container")
    try:
        p = media.probe(real)
    except media.MediaError as exc:
        raise HTTPException(400, str(exc))
    return {
        "path": path, "duration": p.duration,
        "audio": [{"index": a.audio_index, "codec": a.codec, "channels": a.channels,
                   "language": a.language, "title": a.title, "default": a.default,
                   "cleaned": a.is_cleaned} for a in p.audio],
        "has_cleaned": p.cleaned_track is not None,
    }


# ---------------------------------------------------------------------------
#  Jobs
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
def add_jobs(payload: dict):
    """Queue one or many. `items` is [{kind,title,subtitle,path,source,source_id}]."""
    items = payload.get("items") or []
    if not items:
        raise HTTPException(400, "nothing to queue")

    # Cleaning a season, or an episode of a show being watched, is a statement
    # that this show should stay clean - so the show is marked to catch the
    # episodes that have not aired yet. Turned off from the show's page.
    monitor = payload.get("monitor")
    if isinstance(monitor, dict) and monitor.get("source_id"):
        # Marked by cleaning something, so only what arrives from now on: the
        # episodes wanted right now are in this very request, and hoovering up
        # a ten-season back catalogue is not what was asked for.
        db.monitor_add(str(monitor.get("source", "sonarr")),
                       str(monitor["source_id"]), str(monitor.get("title", "")),
                       "new_only", keep_existing_mode=True)
    added, skipped = [], 0
    for item in items:
        path = str(item.get("path") or "")
        if not path:
            continue
        action = "remove" if (item.get("action") or payload.get("action")) == "remove" else "clean"
        job_id = db.enqueue(
            kind=str(item.get("kind", "file")), title=str(item.get("title", Path(path).name)),
            path=path, subtitle=str(item.get("subtitle", "")),
            source=str(item.get("source", "manual")), source_id=str(item.get("source_id", "")),
            force=bool(item.get("force") or payload.get("force")), action=action)
        if job_id is None:
            skipped += 1
        else:
            added.append(job_id)
    worker.nudge()
    return {"queued": len(added), "already_queued": skipped, "ids": added}


@app.get("/api/jobs")
def list_jobs(limit: int = 100, include_finished: bool = False):
    rows = [dict(r) for r in db.recent(limit, include_finished=include_finished)]
    counts = db.count_by_status()
    return {"items": rows, "busy": worker.busy, "holding": worker.holding,
            "override": worker.override, "stats": db.stats(),
            "cancelled": counts.get("cancelled", 0),
            "failed": counts.get("failed", 0),
            "monitors": len(db.monitors()),
            "last_check": worker.last_monitor_check}


@app.post("/api/jobs/{job_id}/move")
def move_job(job_id: int, payload: dict):
    """Reorder the queue: top, bottom, up or down."""
    where = str(payload.get("where", ""))
    if not db.move(job_id, where):
        raise HTTPException(409, "that job cannot move there")
    return {"moved": job_id, "where": where}


@app.post("/api/jobs/move")
def move_jobs(payload: dict):
    """Move several queued jobs at once, as a block."""
    ids = [int(i) for i in (payload.get("ids") or [])]
    where = str(payload.get("where", ""))
    moved = db.move_many(ids, where)
    if not moved:
        raise HTTPException(409, "nothing to move that way")
    return {"moved": moved, "where": where}


@app.post("/api/jobs/cancel")
def cancel_jobs(payload: dict):
    """Cancel several queued jobs at once."""
    ids = [int(i) for i in (payload.get("ids") or [])]
    return {"cancelled": db.cancel_many(ids)}


@app.delete("/api/jobs/cancelled")
def purge_cancelled():
    """Throw away the rows for jobs that were called off."""
    return {"removed": db.purge(("cancelled",))}


@app.delete("/api/jobs/failed")
def purge_failed():
    """Throw away the rows for jobs that failed."""
    return {"removed": db.purge(("failed",))}


@app.post("/api/jobs/retry")
def retry_jobs(payload: dict | None = None):
    """Put failed jobs back in the queue - all of them, or the ones named."""
    ids = [int(i) for i in ((payload or {}).get("ids") or [])]
    count = db.retry_failed(ids or None)
    worker.nudge()
    return {"retrying": count}


@app.delete("/api/queue")
def clear_queue():
    """Cancel everything waiting. Whatever is mid-file is left to finish."""
    return {"cancelled": db.cancel_all_queued()}


@app.post("/api/queue/clean-anyway")
def clean_anyway():
    """Ignore the Plex hold until the queue is empty."""
    worker.override = True
    worker.holding = ""
    worker.nudge()
    return {"override": True}


@app.get("/api/media/sessions")
@app.get("/api/plex/sessions")   # the old name, for a cached page
def plex_sessions():
    """What Plex is doing, and whether that is holding the queue."""
    settings = config.load()
    sessions = arr.sessions_for(settings)
    hold, why = arr.should_hold(sessions, settings.hold_policy)
    return {
        "sessions": [{"who": s.who, "what": s.what, "state": s.state,
                      "video": s.video_decision, "audio": s.audio_decision,
                      "hardware": s.hardware, "transcoding_video": s.transcoding_video,
                      "description": s.describe()} for s in sessions],
        "holding": hold and not worker.override, "why": why,
        "policy": settings.hold_policy,
    }


@app.post("/api/history/remove-all")
def remove_all_cleaned(payload: dict):
    """Queue a removal for every file that currently has a cleaned track.

    Guarded by a phrase the caller has to send back, because this undoes every
    hour the service has ever spent and there is no putting it back except by
    cleaning them all again.
    """
    if str(payload.get("confirm", "")).strip().upper() != "REMOVE ALL":
        raise HTTPException(400, 'send {"confirm": "REMOVE ALL"} to do this')
    queued = 0
    for row in db.history(limit=10000):
        if db.enqueue(kind=row["kind"], title=row["title"],
                      subtitle=f"{row['subtitle']} (removing)", path=row["path"],
                      source=row["source"], source_id=row["source_id"],
                      action="remove"):
            queued += 1
    worker.nudge()
    return {"queued": queued}


@app.get("/api/history")
def history(q: str = "", limit: int = 500):
    """What has been cleaned, for finding a file again afterwards."""
    return {"items": [dict(r) for r in db.history(q, limit)], "stats": db.stats()}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    row = db.get(job_id)
    if row is None:
        raise HTTPException(404, "no such job")
    return {"job": dict(row), "detections": [dict(d) for d in db.detections(job_id)]}


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: int):
    if not worker.cancel(job_id):
        raise HTTPException(409, "that job is not queued or running")
    return {"cancelled": job_id}


@app.get("/api/health")
def health():
    from . import asr as _asr
    return {"ok": True, "gpu": _asr._cuda_available(), "busy": worker.busy}


# ---------------------------------------------------------------------------
#  The page
# ---------------------------------------------------------------------------

@app.middleware("http")
async def _cache_rules(request, call_next):
    """The page must never be stale; artwork should never be refetched.

    A browser holding yesterday's CSS after an update looks exactly like a
    broken layout, and that is a miserable thing to debug from a phone.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/static/sw.js":
        # The worker is served from /static/ but has to control the whole site,
        # and a worker may only claim a scope at or below its own URL unless
        # the server says otherwise. Without this header the registration is
        # rejected and the app is not installable.
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
    elif path.startswith("/static/") and path.endswith((".png", ".ico")):
        response.headers["Cache-Control"] = "public, max-age=604800"
    elif path.startswith("/static/") or path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))
