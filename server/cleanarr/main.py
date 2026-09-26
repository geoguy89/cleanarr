"""The HTTP side: a small API and the page that drives it."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import arr, auth, config, db, judge, library, media, validate, words
from .worker import Worker

WEB_DIR = Path(os.environ.get("CLEANARR_WEB", "/app/web"))
CACHE_DIR = Path(os.environ.get("CLEANARR_CACHE", "/config/cache"))

worker = Worker(CACHE_DIR)

# For the pages that ask Sonarr and Radarr two slow questions at once. Both
# are the remote service waiting on its own database, so they overlap cleanly.
POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cleanarr-arr")


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


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _startup()
    yield
    worker.stop()


app = FastAPI(title="Cleanarr", docs_url="/api/docs", redoc_url=None, lifespan=_lifespan)


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
    if auth.verify(token, settings.auth_secret, settings.auth_user):
        return await call_next(request)
    if request.url.path.startswith(auth.WEBHOOK_PATHS):
        given = auth.basic_credentials(request.headers.get("authorization", ""))
        if (given and given[0] == settings.auth_user
                and auth.check_password(given[1], settings.auth_hash, settings.auth_salt)):
            return await call_next(request)
        return JSONResponse(
            {"error": "set the webhook's username and password to the Cleanarr login"},
            status_code=401, headers={"WWW-Authenticate": 'Basic realm="Cleanarr"'})
    return JSONResponse({"error": "not signed in"}, status_code=401)


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

# name -> total bytes expected, 0 if we could not find out
_downloading: dict[str, int] = {}
# name -> why the last download failed, so the page can say so rather than
# the Download button quietly coming back as if nothing had happened.
_download_errors: dict[str, str] = {}


def _repo(name: str) -> str:
    return f"Systran/faster-whisper-{name}"


def _model_dir(name: str) -> Path:
    return CACHE_DIR / "models" / f"models--Systran--faster-whisper-{name}"


def _files(name: str):
    folder = _model_dir(name)
    if not folder.exists():
        return []
    # Symlinks are skipped, not followed. A Hugging Face cache stores each file
    # once under blobs/ and links to it from snapshots/, so counting both
    # reports every model at twice its real size.
    return [f for f in folder.rglob("*") if f.is_file() and not f.is_symlink()]


def _on_disk(name: str) -> int:
    """Bytes the model occupies, part-downloaded ones included."""
    return sum(f.stat().st_size for f in _files(name))


def _is_complete(name: str) -> bool:
    """Every blob finished writing.

    Hugging Face writes to `<sha>.incomplete` and renames on success, so one
    of those left behind means the download stopped partway. Treating that as
    ready is how a restart mid-download turned into a job failing at load time.
    """
    files = _files(name)
    if not files:
        return False
    return not any(f.name.endswith(".incomplete") for f in files)


def _expected_bytes(name: str) -> int:
    """Total size of the model, asked of Hugging Face once per download.

    Not hardcoded: a number that quietly goes stale produces a bar that stops
    at 94% or races past 100%, which is worse than no bar.
    """
    try:
        resp = httpx.get(f"https://huggingface.co/api/models/{_repo(name)}",
                         params={"blobs": "true"}, timeout=20.0)
        resp.raise_for_status()
        return sum(int(f.get("size") or 0)
                   for f in (resp.json().get("siblings") or []))
    except Exception:  # noqa: BLE001
        return 0


@app.get("/api/hardware")
def hardware():
    """What Whisper will actually run on, and why.

    Worth stating plainly: "auto" is right for almost everyone, but when it
    picks CPU on a machine that has a GPU, the only way to find out used to be
    timing a job.
    """
    from . import asr as _asr
    settings = config.load()
    cuda = _asr._cuda_available()
    chosen = settings.device
    # Facts only. The wording is built in the browser, because it has to
    # describe the choice currently on screen rather than the saved one.
    return {
        "cuda_available": cuda,
        "device": chosen,
        "effective": ("cuda" if cuda else "cpu") if chosen == "auto" else chosen,
        "compute_type": settings.compute_type,
    }


@app.get("/api/models")
def list_models():
    settings = config.load()
    offered = [BUILTIN_MODEL]
    # A model set by hand in config.yaml is the one that will be used, so it
    # is listed as well rather than leaving medium.en to look like the answer.
    if settings.model and settings.model != BUILTIN_MODEL:
        offered.append(settings.model)
    return {
        "selected": settings.model,
        "folder": str(CACHE_DIR / "models"),
        "items": [_model_row(name) for name in offered],
    }


def _pull_model(name: str, folder: str) -> None:
    from faster_whisper.utils import download_model
    download_model(name, cache_dir=folder)


def _model_row(name: str) -> dict:
    here = _on_disk(name)
    total = _downloading.get(name)
    row = {
        "name": name,
        "approx_size": MODEL_SIZES.get(name, ""),
        "bytes": here,
        "ready": _is_complete(name),
        "downloading": name in _downloading,
        "total": total or 0,
        "error": _download_errors.get(name, ""),
    }
    # Only claim a percentage when both numbers are real. A bar that invents
    # its own progress is worse than a spinner.
    if row["downloading"] and total:
        row["percent"] = max(0.0, min(100.0, round(100.0 * here / total, 1)))
    return row


@app.post("/api/models/{name}/download")
def download_model(name: str):
    """Fetch a model now, rather than during the first job."""
    if name in _downloading:
        return {"started": False, "already": True}
    if _is_complete(name):
        return {"started": False, "ready": True}

    def fetch() -> None:
        # Asked before the download starts so the bar has a denominator from
        # the first poll rather than jumping into existence halfway through.
        _downloading[name] = _expected_bytes(name)
        _download_errors.pop(name, None)
        try:
            _pull_model(name, str(CACHE_DIR / "models"))
            print(f"[cleanarr] downloaded speech model {name}", flush=True)
        except Exception as exc:  # noqa: BLE001
            _download_errors[name] = (
                f"could not download {name}: {exc}. The container needs to reach "
                f"huggingface.co")
            print(f"[cleanarr] could not download {name}: {exc}", flush=True)
        finally:
            _downloading.pop(name, None)

    threading.Thread(target=fetch, daemon=True).start()
    return {"started": True}


# ---------------------------------------------------------------------------
#  The second opinion's model
# ---------------------------------------------------------------------------

def _remote_models(url: str, api_key: str) -> dict:
    base = url.strip().rstrip("/")
    if not base:
        raise HTTPException(400, "no Whisper server address set")
    if base.endswith("/v1"):
        base = base[:-3]
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = httpx.get(f"{base}/v1/models", headers=headers, timeout=15.0)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "models": []}

    # OpenAI shape is {"data": [{"id": ...}]}; some servers return a bare list.
    rows = body.get("data") if isinstance(body, dict) else body
    names = [str(r.get("id") or r.get("name") or "") for r in (rows or [])
             if isinstance(r, dict)]
    return {"ok": True, "models": sorted(n for n in names if n)}


@app.get("/api/asr/remote-models")
def remote_models(url: str = ""):
    """What the Whisper server offers, for the dropdown.

    Model names are the server's own - one instance calls it
    "Systran/faster-whisper-medium.en", another "whisper-1", another something
    it made up. Asking beats guessing. A server with no /v1/models is not a
    fault; the field stays free text in that case.
    """
    settings = config.load()
    return _remote_models(url or settings.asr_url, settings.asr_api_key)


@app.post("/api/asr/remote-models")
def remote_models_typed(payload: dict | None = None):
    """The same, with the address and key from the form."""
    settings = config.load()
    payload = payload or {}
    return _remote_models(str(payload.get("url") or settings.asr_url),
                          _typed_secret(payload.get("api_key"), settings.asr_api_key))


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
    payload = payload or {}
    url, problem = validate.url(payload.get("url") or settings.asr_url,
                                "Whisper server address")
    if not url:
        raise HTTPException(400, "no Whisper server address set")
    if problem:
        return {"ok": False, "error": problem}
    model = str(payload.get("model") or settings.asr_remote_model)
    key = _typed_secret(payload.get("api_key"), settings.asr_api_key)
    return _asr.probe_remote(url, model, key)


def _has_model(names: list[str], wanted: str) -> bool:
    """Whether a model list includes the one asked for.

    Ollama lists "qwen3.5:9b"; a name asked for without a tag means ":latest".
    A different tag of the same model is a different model - the 4b is not
    the 9b.
    """
    if not wanted:
        return False
    want = wanted if ":" in wanted else f"{wanted}:latest"
    return any(n == wanted or n == want for n in names)


def _judge_check(url: str, model: str) -> dict:
    """Is the second opinion reachable, and does it have the model?

    Ollama answers /api/tags; an OpenAI-compatible server answers /v1/models
    instead. Either is fine - judge.py speaks both.
    """
    base, problem = validate.url(url, "Second-opinion address")
    if not base:
        return {"reachable": False, "models": [], "has_selected": False,
                "error": "no address set"}
    if problem:
        return {"reachable": False, "models": [], "has_selected": False,
                "error": problem}
    errors = []
    try:
        resp = httpx.get(f"{base}/api/tags", timeout=10.0)
        resp.raise_for_status()
        names = [m.get("name", "") for m in (resp.json().get("models") or [])]
        return {"reachable": True, "api": "ollama", "models": names,
                "has_selected": _has_model(names, model), "selected": model,
                "can_pull": True}
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        errors.append(str(exc))
    listed = _remote_models(base, "")
    if listed["ok"]:
        names = listed["models"]
        return {"reachable": True, "api": "openai", "models": names,
                "has_selected": model in names, "selected": model, "can_pull": False}
    return {"reachable": False, "models": [], "has_selected": False,
            "error": errors[0] if errors else listed.get("error", "no answer")}


@app.get("/api/judge/models")
def judge_models():
    """What the saved second-opinion server already has."""
    settings = config.load()
    if not settings.judge_url:
        return {"reachable": False, "models": [], "has_selected": False}
    return _judge_check(settings.judge_url, settings.judge_model)


@app.post("/api/judge/check")
def judge_check(payload: dict | None = None):
    """The same, for the address and model typed into the form."""
    settings = config.load()
    payload = payload or {}
    url = payload.get("url")
    model = payload.get("model")
    return _judge_check(settings.judge_url if url is None else str(url),
                        str(model or settings.judge_model))


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
        _download_errors.pop("__ollama__", None)
        try:
            # Streamed, so Ollama does not time the request out on a long
            # download; the body is drained and discarded.
            with httpx.stream("POST", f"{settings.judge_url.rstrip('/')}/api/pull",
                              json={"model": settings.judge_model},
                              timeout=None) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if '"error"' in line:
                        _download_errors["__ollama__"] = line[:300]
            print(f"[cleanarr] Ollama pulled {settings.judge_model}", flush=True)
        except Exception as exc:  # noqa: BLE001
            _download_errors["__ollama__"] = str(exc)
            print(f"[cleanarr] Ollama pull failed: {exc}", flush=True)
        finally:
            _downloading.pop("__ollama__", None)

    threading.Thread(target=fetch, daemon=True).start()
    return {"started": True, "model": settings.judge_model}


@app.get("/api/judge/pull")
def judge_pull_state():
    return {"downloading": _downloading.get("__ollama__", ""),
            "error": _download_errors.get("__ollama__", "")}


# ---------------------------------------------------------------------------
#  Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    settings = config.load()
    data = settings.public()
    data["available_categories"] = [
        {"key": k, "label": words.CATEGORY_LABELS[k],
         "count": len(words.WORDLISTS[k])
         + (len(words.BLASPHEMY_SOLO) + len(words.BLASPHEMY_PHRASES)
            if k == "blasphemy" else 0)}
        for k in words.CATEGORIES]
    data["builtin_model"] = BUILTIN_MODEL
    data["hold_policies"] = arr.hold_policies(settings.media_server)
    return data


def _refuse(errors: dict[str, str]) -> JSONResponse:
    """A 400 naming every field that was wrong, with the first as `detail`."""
    return JSONResponse({"detail": next(iter(errors.values())), "errors": errors},
                        status_code=400)


@app.put("/api/settings")
def put_settings(payload: dict):
    payload, errors = validate.check(payload)
    if errors:
        return _refuse(errors)
    settings = config.load()
    old_title = settings.track_title
    for section in ("sonarr", "radarr"):
        if section in payload and isinstance(payload[section], dict):
            current = getattr(settings, section)
            for key, value in payload[section].items():
                # A masked key means "leave it alone", so the UI can save the
                # form without ever holding the real key.
                if key == "api_key" and _masked(value):
                    continue
                setattr(current, key, value)
    for key in ("categories", "custom_words", "allow_words", "pad_start", "pad_end",
                "fade", "model", "device", "compute_type", "trim_silence", "keep_backup",
                "asr_backend", "asr_url", "asr_remote_model",
                "track_title", "plex_url", "judge_url", "judge_model",
                "media_server", "jellyfin_url", "library_source",
                "judge_threads", "judge_keep_alive", "bitrate_surround",
                "bitrate_stereo", "ffmpeg_threads", "hold_policy",
                "check_in_context"):
        if key in payload:
            setattr(settings, key, payload[key])
    # Same masking rule for every secret: all-stars means "leave it".
    for key in ("plex_token", "asr_api_key", "jellyfin_api_key"):
        if key in payload and not _masked(payload[key]):
            setattr(settings, key, payload[key])
    # An empty box means the default, not a nameless track. The old name is
    # kept because detection matches every name this install has used: after a
    # rename, re-cleaning an older file still replaces its track rather than
    # adding a second, and removing the cleaned track still finds it.
    settings.track_title = (str(settings.track_title or "").strip()
                            or media.DEFAULT_CLEAN_TITLE)
    # Choices that only make sense together. Checked on the merged result, so
    # saving one section at a time still works.
    if settings.asr_backend == "remote" and not settings.asr_url:
        return _refuse({"asr_url": "Using your own Whisper server needs its address"})
    if settings.judge_url and not settings.judge_model:
        return _refuse({"judge_model": "Name the model the second opinion should use"})
    if old_title and settings.track_title != old_title:
        known = [t for t in settings.known_track_titles
                 if t and t != settings.track_title]
        if old_title not in known:
            known.append(old_title)
        settings.known_track_titles = known
    config.save(settings)
    return config.load().public()


def _masked(value) -> bool:
    """A secret as the settings page shows it: all stars, standing for the saved one."""
    return set(str(value)) == {"*"}


def _typed_secret(typed, saved: str) -> str:
    """What the form holds for a secret: the masked value means "the saved one"."""
    text = str(typed or "").strip()
    return saved if not text or _masked(text) else text


@app.post("/api/settings/test/{service}")
def test_service(service: str, payload: dict | None = None):
    """Try a connection with what is in the form, saved or not.

    Testing the saved values instead meant typing an address, pressing Test and
    being told about the old one - which is the moment somebody is most likely
    to be setting it up for the first time.
    """
    settings = config.load()
    payload = payload or {}
    saved = {
        "sonarr": (settings.sonarr.url, settings.sonarr.api_key),
        "radarr": (settings.radarr.url, settings.radarr.api_key),
        "plex": (settings.plex_url, settings.plex_token),
        "jellyfin": (settings.jellyfin_url, settings.jellyfin_api_key),
    }
    if service not in saved:
        raise HTTPException(404, "no such service")
    saved_url, saved_key = saved[service]
    typed_url = payload.get("url")
    address, problem = validate.url(saved_url if typed_url is None else typed_url,
                                    f"{service.title()} address")
    if problem:
        return {"ok": False, "error": problem}
    key = _typed_secret(payload.get("api_key"), saved_key)
    try:
        if service == "sonarr":
            return arr.Sonarr(address, key).test()
        if service == "radarr":
            return arr.Radarr(address, key).test()
        if service == "plex":
            return library.PlexLibrary(address, key).test()
        return library.JellyfinLibrary(address, key).test()
    except (library.LibraryError, arr.ArrError) as exc:
        return {"ok": False, "error": str(exc)}


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
    by_title = db.job_titles()
    watched = {row["source_id"] for row in db.monitors("sonarr")}
    for show in items:
        # Plex does not report a show's folder, so its jobs are matched on the
        # show's name instead - which is what every episode job is titled.
        folder = (show.get("path") or "").rstrip("/") + "/"
        if folder != "/":
            statuses = [s for p, s in known.items() if p.startswith(folder)]
        else:
            statuses = [s for _p, s in by_title.get(show.get("title", ""), [])]
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
    return {"items": items, "library_source": settings.library_source}


def _attach_jobs(items: list[dict], known: dict[str, dict],
                 with_size: bool = False) -> None:
    """Mark each episode or film with what this service last did to its file."""
    for item in items:
        job = known.get(item["path"]) or {}
        item["job_status"] = job.get("status", "")
        item["job_id"] = job.get("job_id")
        item["muted"] = job.get("muted")
        item["cleaned_at"] = job.get("finished_at")
        if with_size:
            item["added_bytes"] = job.get("added_bytes")


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
        _attach_jobs(group, known, with_size=True)
        for item in group:
            item["source"] = _poster_source(settings, kind)

    counts = db.count_by_status()
    return {
        "library_source": settings.library_source,
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
    _attach_jobs(items, db.cleaned_paths([e["path"] for e in items]), with_size=True)
    return {"items": items}


@app.get("/api/movies")
def movies():
    settings = config.load()
    try:
        items = library.build(settings).movies()
    except (arr.ArrError, library.LibraryError) as exc:
        raise HTTPException(502, str(exc))
    _attach_jobs(items, db.cleaned_paths([m["path"] for m in items]), with_size=True)
    for movie in items:
        movie["latest"] = movie.get("added", "")

    source = _poster_source(settings, "movie")
    for movie in items:
        movie["source"] = source
    threading.Thread(target=_warm_posters,
                     args=(source, [m["id"] for m in items]),
                     daemon=True).start()
    return {"items": items, "library_source": settings.library_source}


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
#  The path check
#
#  The library reports paths as IT sees them, and this container opens exactly
#  those. They agree when the media is mounted here at the path the library
#  reports, and not otherwise: a media server on another host, or one reaching
#  storage over its own mount, reports paths that mean nothing here. The
#  symptom is the library browsing perfectly while every job fails with "not
#  found from this container".
#
#  The fix is the mount: the media has to be reachable from this container in
#  any case, mounted at the path the library reports. So this reports what the
#  library said, whether it is openable, and where the same folder appears to
#  be if it is mounted somewhere else. There is no path rewriting.
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

    Deepest ancestor first, so the answer is the most specific one that
    resolves rather than a shallow coincidence. Two components have to match,
    because one does not mean anything: every machine has some directory
    called "media" or "tv" somewhere, and pointing at it as though it were the
    library is worse than saying nothing. A one-component path is all there is
    to match, so it is allowed to match on its own.
    """
    parts = [p for p in reported.strip("/").split("/") if p]
    least = 1 if len(parts) < 2 else 2
    for end in range(len(parts), 0, -1):
        prefix = parts[:end]
        for root in visible:
            for start in range(len(prefix) - least + 1):
                candidate = Path(root, *prefix[start:])
                try:
                    if candidate.is_dir():
                        return "/" + "/".join(prefix), str(candidate)
                except OSError:
                    continue
    return "", ""


@app.get("/api/paths")
def path_report():
    """Where the library says the media is, and whether we can open it."""
    settings = config.load()
    visible = _visible_roots()
    try:
        roots = library.build(settings).roots()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "visible": visible, "roots": []}

    out = []
    for r in roots:
        reported = r.get("path", "")
        ok = Path(reported).is_dir()
        row = {**r, "ok": ok, "elsewhere": ""}
        if not ok:
            # Mounted, but somewhere else: the fix is then one line of the
            # compose file rather than a new share.
            _matched, found = _suggest(reported, visible)
            row["elsewhere"] = found
        out.append(row)
    return {"roots": out, "visible": visible,
            "source": getattr(settings, "library_source", "arr")}


# ---------------------------------------------------------------------------
#  First run
#
#  What is left to set up, in the order it has to happen, for the checklist on
#  Home. Each item says what is wrong in words and which Settings section fixes
#  it, so a new install is never a blank page with no hint of why.
# ---------------------------------------------------------------------------

def _library_configured(settings) -> tuple[bool, str]:
    source = settings.library_source
    if source == "plex":
        return bool(settings.plex_url and settings.plex_token), "Plex address and token"
    if source == "jellyfin":
        return (bool(settings.jellyfin_url and settings.jellyfin_api_key),
                "Jellyfin address and API key")
    has_any = (settings.sonarr.url and settings.sonarr.api_key) or (
        settings.radarr.url and settings.radarr.api_key)
    return bool(has_any), "Sonarr or Radarr address and API key"


def _check_library(settings) -> dict:
    item = {"key": "library", "title": "Connect your library", "section": "library"}
    configured, needs = _library_configured(settings)
    if not configured:
        return {**item, "state": "todo", "detail": f"Add the {needs}."}
    try:
        if settings.library_source == "plex":
            result = library.PlexLibrary(settings.plex_url, settings.plex_token).test()
        elif settings.library_source == "jellyfin":
            result = library.JellyfinLibrary(settings.jellyfin_url,
                                             settings.jellyfin_api_key).test()
        else:
            answered = []
            for name, cfg, client in (("Sonarr", settings.sonarr, arr.Sonarr),
                                      ("Radarr", settings.radarr, arr.Radarr)):
                if cfg.url and cfg.api_key:
                    client(cfg.url, cfg.api_key).test()
                    answered.append(name)
            missing = [n for n in ("Sonarr", "Radarr") if n not in answered]
            detail = f"{' and '.join(answered)} answered."
            if missing:
                detail += (" Radarr is not set, so films will not show."
                           if missing == ["Radarr"]
                           else " Sonarr is not set, so shows will not show.")
            return {**item, "state": "ok", "detail": detail}
    except (arr.ArrError, library.LibraryError) as exc:
        return {**item, "state": "problem", "detail": str(exc)}
    return {**item, "state": "ok", "detail": f"{result['app']} answered."}


def _check_paths(settings, library_ok: bool) -> dict:
    item = {"key": "paths", "title": "Make the media reachable", "section": "library"}
    if not library_ok:
        return {**item, "state": "todo",
                "detail": "Checked once the library is connected."}
    try:
        roots = library.build(settings).roots()
    except Exception as exc:  # noqa: BLE001
        return {**item, "state": "problem", "detail": str(exc)}
    if not roots:
        return {**item, "state": "todo",
                "detail": "The library reported no folders yet."}
    bad = [r["path"] for r in roots if not Path(r["path"]).is_dir()]
    if bad:
        return {**item, "state": "problem",
                "detail": f"{len(bad)} of {len(roots)} library folders cannot be "
                          f"opened from this container, starting with {bad[0]}. "
                          f"Mount them at the same paths."}
    return {**item, "state": "ok",
            "detail": f"All {len(roots)} library folders open from here."}


def _check_listening(settings) -> dict:
    from . import asr as _asr
    item = {"key": "listening", "title": "Get the speech model", "section": "listening"}
    if settings.asr_backend == "remote":
        if not settings.asr_url:
            return {**item, "state": "todo", "detail": "Add your Whisper server's address."}
        return {**item, "state": "ok",
                "detail": "Using your own Whisper server. Test it under Listening."}
    if settings.device == "cuda" and not _asr._cuda_available():
        return {**item, "state": "problem",
                "detail": "Set to the NVIDIA GPU, but this container can see none."}
    name = settings.model or BUILTIN_MODEL
    if name in _downloading:
        row = _model_row(name)
        pct = f" {row['percent']:.0f}%" if "percent" in row else ""
        return {**item, "state": "todo", "detail": f"Downloading{pct}…"}
    if _download_errors.get(name):
        return {**item, "state": "problem", "detail": _download_errors[name]}
    if not _is_complete(name):
        return {**item, "state": "todo",
                "detail": f"Download {name} now, rather than during the first clean."}
    return {**item, "state": "ok", "detail": f"{name} is downloaded."}


def _check_first_clean() -> dict:
    item = {"key": "first_clean", "title": "Clean one episode", "section": "shows"}
    counts = db.count_by_status()
    if counts.get("done") or counts.get("skipped"):
        return {**item, "state": "ok", "detail": "Done at least once."}
    if counts.get("queued") or counts.get("running"):
        return {**item, "state": "todo", "detail": "One is in the queue."}
    return {**item, "state": "todo",
            "detail": "Try one episode before turning it loose on a season."}


@app.get("/api/setup")
def setup_status():
    """The first-run checklist: required steps, then optional ones."""
    settings = config.load()
    lib = _check_library(settings)
    required = [lib, _check_paths(settings, lib["state"] == "ok"),
                _check_listening(settings), _check_first_clean()]
    optional = [{
        "key": "login", "title": "Set a password", "section": "security",
        "state": "ok" if settings.auth_user else "info",
        "detail": ("Login is on." if settings.auth_user else
                   "Anyone who can reach this address can use it."),
    }]
    checked = [w for w in settings.check_in_context if w.strip()]
    if checked and not settings.judge_url:
        optional.append({
            "key": "judge", "title": "Second opinion", "section": "judge",
            "state": "info",
            "detail": f"{len(checked)} words are on the check-in-context list, but no "
                      f"second opinion is set, so they are muted outright.",
        })
    return {"required": required, "optional": optional,
            "done": all(i["state"] == "ok" for i in required)}


@app.get("/api/file")
def file_info(path: str):
    """What is in one file: used to show existing tracks before queueing."""
    settings = config.load()
    # Same names the pipeline will use, so a track this install cleaned under
    # an older name is still shown as cleaned here.
    media.set_clean_title(settings.track_title, settings.known_track_titles)
    real = Path(path)
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


# ---------------------------------------------------------------------------
#  Correcting a wrong call
#
#  Whisper hears a name as a swear, or the judge clears a word it should not
#  have. Either way the fix is a word list, so a detection can put its word on
#  one directly instead of someone copying it into Settings by hand.
# ---------------------------------------------------------------------------

# Which setting each list is.
WORD_LISTS = {"never": "allow_words", "context": "check_in_context",
              "always": "custom_words"}


def _has(items: list[str], word: str) -> bool:
    return any(words.normalize(w) == word for w in items)


def _without(items: list[str], word: str) -> list[str]:
    return [w for w in items if words.normalize(w) != word]


@app.post("/api/detections/{detection_id}/correct")
def correct_detection(detection_id: int, payload: dict):
    """Put a detection's word on a list so the next clean gets it right.

    never:   never mute it.
    context: mute it unless the second opinion says it was an ordinary word.
    always:  mute it wherever it is heard - for a word the second opinion let
             through. Takes it off the other two lists.
    """
    row = db.detection(detection_id)
    if row is None:
        raise HTTPException(404, "no such detection")
    target = str(payload.get("list", ""))
    if target not in WORD_LISTS:
        raise HTTPException(400, "list must be never, context or always")
    word = words.normalize(row["text"])
    if not word:
        raise HTTPException(400, "that detection has no word to add")

    settings = config.load()
    added = False
    if target == "always":
        settings.allow_words = _without(settings.allow_words, word)
        settings.check_in_context = _without(settings.check_in_context, word)
        # Already on a built-in list: taking it off the other two is enough.
        builtin = words.Matcher(categories=tuple(settings.categories),
                                never=frozenset(), context_words=frozenset())
        if (not builtin.find([{"word": word, "start": 0.0, "end": 0.1}])
                and not _has(settings.custom_words, word)):
            settings.custom_words = [*settings.custom_words, word]
        added = True
    else:
        field_name = WORD_LISTS[target]
        current = list(getattr(settings, field_name))
        if not _has(current, word):
            setattr(settings, field_name, [*current, word])
            added = True
        if target == "never":
            settings.custom_words = _without(settings.custom_words, word)
    config.save(settings)
    return {"word": word, "list": target, "added": added, "job_id": row["job_id"],
            "judge_configured": bool(settings.judge_url)}


@app.delete("/api/words/{list_name}/{word}")
def remove_word(list_name: str, word: str):
    """Take a word back off a list - the undo for a correction."""
    if list_name not in WORD_LISTS:
        raise HTTPException(404, "no such list")
    settings = config.load()
    field_name = WORD_LISTS[list_name]
    before = list(getattr(settings, field_name))
    after = _without(before, words.normalize(word))
    setattr(settings, field_name, after)
    config.save(settings)
    return {"removed": len(before) - len(after)}


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
    elif path.startswith("/static/") and path.endswith((".png", ".ico", ".svg")):
        response.headers["Cache-Control"] = "public, max-age=604800"
    elif path.startswith("/static/") or path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))
