"""Where the media actually is.

Cleanarr was built against Sonarr and Radarr, which was a reasonable place to
start - they know every file's path, which is the one thing this needs. But
plenty of people run Plex or Jellyfin and nothing else, and telling them to
install two more services to mute swearing is not a reasonable answer.

So the library is a source with three implementations. All any of them has to
do is answer four questions:

    what shows are there          what episodes does this show have
    what films are there          what does this thing look like

Everything downstream - the queue, the pipeline, the word lists - only ever
sees a path, so it does not care which of these answered.

WHAT IS LOST WITHOUT SONARR
---------------------------
The Upcoming calendar. Neither Plex nor Jellyfin knows what has not aired yet,
because neither is a downloader. The tab hides itself rather than showing an
empty page and inviting the question.

Everything else survives, including cleaning new episodes automatically: both
servers record when an item was added, which is the same signal Sonarr's
import history gives.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

import httpx

TIMEOUT = 30.0


class LibraryError(RuntimeError):
    pass


def jellyfin_headers(api_key: str) -> dict:
    """Both auth schemes, because Jellyfin changed which one it accepts.

    Jellyfin 12 rejects X-Emby-Token and api_key= outright (401) and takes only
    the MediaBrowser Authorization header. Older servers accept either. Sending
    both costs nothing and works on every version.
    """
    return {
        "Authorization": f'MediaBrowser Token="{api_key}", '
                         'Client="Cleanarr", Device="Cleanarr", '
                         'DeviceId="cleanarr", Version="1.0"',
        "X-Emby-Token": api_key,
    }


def _iso(epoch: int | float | None) -> str:
    """Plex counts seconds since 1970; everything here speaks ISO-8601."""
    if not epoch:
        return ""
    return _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
#  Plex
# ---------------------------------------------------------------------------

@dataclass
class PlexLibrary:
    url: str
    token: str

    name = "plex"

    def _get(self, path: str, **params) -> dict:
        if not self.url or not self.token:
            raise LibraryError("Plex address and token are not set")
        params["X-Plex-Token"] = self.token
        try:
            resp = httpx.get(f"{self.url.rstrip('/')}{path}", params=params,
                             headers={"Accept": "application/json"}, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise LibraryError(f"could not reach Plex: {exc}") from exc
        if resp.status_code == 401:
            raise LibraryError("Plex rejected the token")
        if resp.status_code >= 400:
            raise LibraryError(f"Plex said {resp.status_code}")
        try:
            return resp.json().get("MediaContainer") or {}
        except ValueError as exc:
            raise LibraryError(f"{self.url} did not answer like Plex - check the "
                               f"address") from exc

    def _sections(self, kind: str) -> list[str]:
        """Section keys of a given type - "show" or "movie"."""
        return [str(d["key"]) for d in (self._get("/library/sections").get("Directory") or [])
                if d.get("type") == kind]

    def _all(self, section: str, **params) -> list[dict]:
        """Every item in a section, paged.

        Plex will return a whole library in one response if asked, and on a
        thousand-show library that is a large amount of JSON to build and parse
        at once. Paging keeps the memory flat and lets a slow server answer.
        """
        out: list[dict] = []
        start, page = 0, 500
        while True:
            container = self._get(f"/library/sections/{section}/all",
                                  **{**params,
                                     "X-Plex-Container-Start": start,
                                     "X-Plex-Container-Size": page})
            batch = container.get("Metadata") or []
            out.extend(batch)
            total = int(container.get("totalSize") or len(out))
            start += page
            if len(batch) < page or start >= total:
                return out

    def shows(self) -> list[dict]:
        out = []
        for section in self._sections("show"):
            for s in self._all(section):
                out.append({
                    "id": str(s.get("ratingKey")),
                    "title": s.get("title", ""),
                    "year": s.get("year"),
                    # Plex does not give a show's folder in the listing, and it
                    # is not needed: the folder is only used to match jobs, and
                    # episodes carry their own full paths.
                    "path": "",
                    "episodes": int(s.get("leafCount") or 0),
                    "added": _iso(s.get("addedAt")),
                    "status": "",
                })
        return out

    def episodes(self, show_id: str) -> list[dict]:
        out = []
        for e in (self._get(f"/library/metadata/{show_id}/allLeaves").get("Metadata") or []):
            part = (((e.get("Media") or [{}])[0].get("Part") or [{}])[0])
            path = part.get("file") or ""
            if not path:
                continue          # an episode Plex knows of but has no file for
            out.append({
                "id": str(e.get("ratingKey")),
                "season": int(e.get("parentIndex") or 0),
                "episode": int(e.get("index") or 0),
                "title": e.get("title", ""),
                "path": path,
                "size": int(part.get("size") or 0),
                "quality": "",
                "added": _iso(e.get("addedAt")),
            })
        out.sort(key=lambda e: (e["season"], e["episode"]))
        return out

    def movies(self) -> list[dict]:
        out = []
        for section in self._sections("movie"):
            for m in self._all(section):
                part = (((m.get("Media") or [{}])[0].get("Part") or [{}])[0])
                path = part.get("file") or ""
                if not path:
                    continue
                out.append({
                    "id": str(m.get("ratingKey")),
                    "title": m.get("title", ""),
                    "year": m.get("year"),
                    "path": path,
                    "size": int(part.get("size") or 0),
                    "quality": "",
                    "added": _iso(m.get("addedAt")),
                })
        out.sort(key=lambda m: (m["title"] or "").lower())
        return out

    def poster(self, item_id: str) -> bytes:
        """Artwork, already scaled - a poster wall does not need 2000px."""
        meta = self._get(f"/library/metadata/{item_id}")
        items = meta.get("Metadata") or []
        thumb = items[0].get("thumb") if items else None
        if not thumb:
            raise LibraryError("no artwork")
        resp = httpx.get(f"{self.url.rstrip('/')}{thumb}",
                         params={"X-Plex-Token": self.token}, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.content

    def recent_episodes(self, limit: int = 12) -> list[dict]:
        """What landed lately, for the Home page.

        Plex keeps its own "recently added" per section, which is both quicker
        and more accurate than sorting a whole library by date here.
        """
        out = []
        for section in self._sections("show"):
            container = self._get(f"/library/sections/{section}/recentlyAdded",
                                  **{"X-Plex-Container-Start": 0,
                                     "X-Plex-Container-Size": limit})
            for e in (container.get("Metadata") or []):
                part = (((e.get("Media") or [{}])[0].get("Part") or [{}])[0])
                if not part.get("file"):
                    continue
                out.append({
                    "series_id": str(e.get("grandparentRatingKey") or ""),
                    "episode_id": str(e.get("ratingKey")),
                    "series": e.get("grandparentTitle", ""),
                    "network": "",
                    "season": int(e.get("parentIndex") or 0),
                    "episode": int(e.get("index") or 0),
                    "title": e.get("title", ""),
                    "path": part["file"],
                    "added": _iso(e.get("addedAt")),
                    "size": int(part.get("size") or 0),
                    "quality": "",
                })
        out.sort(key=lambda e: e["added"], reverse=True)
        return out[:limit]

    def roots(self) -> list[dict]:
        """Every folder Plex has a library in, as Plex sees it.

        Plex reports paths from inside its own container, which need not match
        ours; these are what the path check in Settings compares against.
        """
        out = []
        for d in (self._get("/library/sections").get("Directory") or []):
            if d.get("type") not in ("show", "movie"):
                continue
            for loc in (d.get("Location") or []):
                if loc.get("path"):
                    out.append({"library": d.get("title", ""),
                                "kind": d.get("type"),
                                "path": loc["path"]})
        return out

    def test(self) -> dict:
        container = self._get("/library/sections")
        kinds = [d.get("type") for d in (container.get("Directory") or [])]
        return {"ok": True, "app": "Plex",
                "version": f"{kinds.count('show')} TV, {kinds.count('movie')} film libraries"}


# ---------------------------------------------------------------------------
#  Jellyfin
# ---------------------------------------------------------------------------

@dataclass
class JellyfinLibrary:
    url: str
    api_key: str

    name = "jellyfin"

    def _get(self, path: str, **params):
        if not self.url or not self.api_key:
            raise LibraryError("Jellyfin address and API key are not set")
        try:
            resp = httpx.get(f"{self.url.rstrip('/')}{path}", params=params,
                             headers={**jellyfin_headers(self.api_key),
                                      "Accept": "application/json"},
                             timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise LibraryError(f"could not reach Jellyfin: {exc}") from exc
        if resp.status_code in (401, 403):
            raise LibraryError("Jellyfin rejected the API key")
        if resp.status_code >= 400:
            raise LibraryError(f"Jellyfin said {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LibraryError(f"{self.url} did not answer like Jellyfin - check the "
                               f"address") from exc

    def _items(self, **params) -> list[dict]:
        params.setdefault("Recursive", "true")
        params.setdefault("Fields", "Path,DateCreated,ProviderIds")
        # Jellyfin will page, and a large library in one response is slow to
        # build server-side as well as to parse here.
        out: list[dict] = []
        start, page = 0, 500
        while True:
            body = self._get("/Items", StartIndex=start, Limit=page, **params)
            batch = body.get("Items") or []
            out.extend(batch)
            total = int(body.get("TotalRecordCount") or len(out))
            start += page
            if len(batch) < page or start >= total:
                return out

    def shows(self) -> list[dict]:
        return [{
            "id": str(s.get("Id")),
            "title": s.get("Name", ""),
            "year": s.get("ProductionYear"),
            "path": s.get("Path") or "",
            "episodes": int(s.get("RecursiveItemCount") or 0),
            "added": (s.get("DateCreated") or "")[:19],
            "status": s.get("Status", ""),
        } for s in self._items(IncludeItemTypes="Series",
                               Fields="Path,DateCreated,RecursiveItemCount")]

    def episodes(self, show_id: str) -> list[dict]:
        out = []
        for e in self._items(ParentId=show_id, IncludeItemTypes="Episode",
                             Fields="Path,DateCreated,MediaSources"):
            path = e.get("Path") or ""
            if not path:
                continue
            sources = e.get("MediaSources") or [{}]
            out.append({
                "id": str(e.get("Id")),
                "season": int(e.get("ParentIndexNumber") or 0),
                "episode": int(e.get("IndexNumber") or 0),
                "title": e.get("Name", ""),
                "path": path,
                "size": int(sources[0].get("Size") or 0),
                "quality": "",
                "added": (e.get("DateCreated") or "")[:19],
            })
        out.sort(key=lambda e: (e["season"], e["episode"]))
        return out

    def movies(self) -> list[dict]:
        out = []
        for m in self._items(IncludeItemTypes="Movie",
                             Fields="Path,DateCreated,MediaSources"):
            path = m.get("Path") or ""
            if not path:
                continue
            sources = m.get("MediaSources") or [{}]
            out.append({
                "id": str(m.get("Id")),
                "title": m.get("Name", ""),
                "year": m.get("ProductionYear"),
                "path": path,
                "size": int(sources[0].get("Size") or 0),
                "quality": "",
                "added": (m.get("DateCreated") or "")[:19],
            })
        out.sort(key=lambda m: (m["title"] or "").lower())
        return out

    def poster(self, item_id: str) -> bytes:
        try:
            resp = httpx.get(f"{self.url.rstrip('/')}/Items/{item_id}/Images/Primary",
                             params={"maxWidth": 400},
                             headers=jellyfin_headers(self.api_key), timeout=TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LibraryError(f"no artwork: {exc}") from exc
        return resp.content

    def recent_episodes(self, limit: int = 12) -> list[dict]:
        out = []
        rows = self._get("/Items/Latest", IncludeItemTypes="Episode",
                         Limit=limit, Fields="Path,DateCreated,MediaSources")
        for e in (rows or []):
            path = e.get("Path") or ""
            if not path:
                continue
            sources = e.get("MediaSources") or [{}]
            out.append({
                "series_id": str(e.get("SeriesId") or ""),
                "episode_id": str(e.get("Id")),
                "series": e.get("SeriesName", ""),
                "network": "",
                "season": int(e.get("ParentIndexNumber") or 0),
                "episode": int(e.get("IndexNumber") or 0),
                "title": e.get("Name", ""),
                "path": path,
                "added": (e.get("DateCreated") or "")[:19],
                "size": int(sources[0].get("Size") or 0),
                "quality": "",
            })
        return out

    def roots(self) -> list[dict]:
        """Folders Jellyfin has libraries in, as Jellyfin sees them."""
        out = []
        for f in (self._get("/Library/VirtualFolders") or []):
            kind = {"tvshows": "show", "movies": "movie"}.get(
                f.get("CollectionType", ""), f.get("CollectionType", ""))
            if kind not in ("show", "movie"):
                continue
            for path in (f.get("Locations") or []):
                out.append({"library": f.get("Name", ""), "kind": kind, "path": path})
        return out

    def test(self) -> dict:
        info = self._get("/System/Info")
        return {"ok": True, "app": info.get("ServerName") or "Jellyfin",
                "version": info.get("Version", "")}


# ---------------------------------------------------------------------------
#  Sonarr and Radarr, wearing the same interface
# ---------------------------------------------------------------------------

@dataclass
class ArrLibrary:
    """The original source. Kept as the default because it knows more.

    Sonarr can say what has not aired yet, which neither media server can, so
    this is the only source that can fill the Upcoming calendar.
    """

    sonarr: object
    radarr: object

    name = "arr"
    has_calendar = True

    def shows(self) -> list[dict]:
        return self.sonarr.series()

    def episodes(self, show_id: str) -> list[dict]:
        return self.sonarr.episodes(int(show_id))

    def movies(self) -> list[dict]:
        return self.radarr.movies()

    def recent_episodes(self, limit: int = 12) -> list[dict]:
        return self.sonarr.recent_imports(limit)

    def roots(self) -> list[dict]:
        """Root folders, as each *arr app sees them."""
        out = []
        for app, kind in ((self.sonarr, "show"), (self.radarr, "movie")):
            for r in (getattr(app, "root_folders", lambda: [])() or []):
                if r.get("path"):
                    out.append({"library": "Sonarr" if kind == "show" else "Radarr",
                                "kind": kind, "path": r["path"]})
        return out


PlexLibrary.has_calendar = False
JellyfinLibrary.has_calendar = False


def build(settings):
    """Whichever source the settings name."""
    source = getattr(settings, "library_source", "arr")
    if source == "plex":
        return PlexLibrary(settings.plex_url, settings.plex_token)
    if source == "jellyfin":
        return JellyfinLibrary(settings.jellyfin_url, settings.jellyfin_api_key)
    from . import arr as _arr
    return ArrLibrary(_arr.Sonarr(settings.sonarr.url, settings.sonarr.api_key),
                      _arr.Radarr(settings.radarr.url, settings.radarr.api_key))
