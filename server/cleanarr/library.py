"""Where the media actually is.

Cleanarr was built against Sonarr and Radarr, which was a reasonable place to
start - they know every file's path, which is the one thing this needs. But
plenty of people run Plex or Jellyfin and nothing else, and telling them to
install two more services to mute swearing is not a reasonable answer.

Films always come from the media server. Radarr was dropped: it knew nothing
about a film that the media server does not, and the media server also knows
each film's other versions and any subtitles it downloaded. Shows come from
Sonarr when it is set up, for its calendar, or else from the media server.

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
import re
from dataclasses import dataclass

import httpx

TIMEOUT = 30.0
# An online subtitle search goes out to OpenSubtitles and back.
SEARCH_TIMEOUT = 90.0


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


_TEXT_CODECS = {"srt", "subrip", "vtt", "webvtt"}


def _english(tag) -> bool:
    return str(tag or "").strip().lower() in ("en", "eng", "english")


def _forced(flag, title) -> bool:
    """Forced subtitles carry only the foreign-language lines - useless here."""
    return flag in (True, 1, "1", "true") or "forced" in str(title or "").lower()


def _parts(path: str) -> list[str]:
    return [p for p in str(path or "").replace("\\", "/").split("/") if p]


def same_file(a: str, b: str) -> bool:
    """Whether two paths name the same file as two programs see it.

    The media server often has the library mounted somewhere else than Sonarr
    and this container do - /data/tv against /tv - so an exact match is tried
    first, then the file name and its folder, which inside one show or film
    library is only ever one file.
    """
    if a == b:
        return bool(a)
    pa, pb = _parts(a), _parts(b)
    if len(pa) < 2 or len(pb) < 2:
        return False
    return [x.lower() for x in pa[-2:]] == [x.lower() for x in pb[-2:]]


def local_path(theirs: str, their_here: str, our_here: str) -> str:
    """`theirs`, a path as the media server sees it, as this container sees it -
    by swapping the prefix the two paths of one known file differ by."""
    if their_here == our_here:
        return theirs
    a, b = _parts(their_here), _parts(our_here)
    common = 0
    while common < min(len(a), len(b)) and a[-1 - common] == b[-1 - common]:
        common += 1
    if common == 0:
        return theirs
    prefix, ours = a[:len(a) - common], b[:len(b) - common]
    t = _parts(theirs)
    if t[:len(prefix)] != prefix:
        return theirs
    return "/" + "/".join(ours + t[len(prefix):])


def title_variants(title: str) -> list[str]:
    """The show's name as Sonarr has it, and without a trailing year or country:
    Sonarr says "Doctor Who (2005)" where Plex says "Doctor Who"."""
    title = str(title or "").strip()
    bare = re.sub(r"\s*\((?:\d{4}|[A-Za-z]{2,3})\)\s*$", "", title).strip()
    return [t for t in dict.fromkeys((title, bare)) if t]


def _pick(items: list[dict], path: str) -> str:
    """The id of the item whose file is `path`: the exact path if one has it,
    else the one file whose name and folder match."""
    exact = [i["id"] for i in items if i.get("path") == path]
    if exact:
        return exact[0]
    near = [i["id"] for i in items if same_file(i.get("path", ""), path)]
    return near[0] if len(near) == 1 else ""


# ---------------------------------------------------------------------------
#  Plex
# ---------------------------------------------------------------------------

@dataclass
class PlexLibrary:
    url: str
    token: str

    name = "plex"

    def _get(self, path: str, timeout: float = 0, **params) -> dict:
        if not self.url or not self.token:
            raise LibraryError("Plex address and token are not set")
        params["X-Plex-Token"] = self.token
        try:
            resp = httpx.get(f"{self.url.rstrip('/')}{path}", params=params,
                             headers={"Accept": "application/json"},
                             timeout=timeout or TIMEOUT)
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

    def versions(self, item_id: str) -> list[dict]:
        """Every file of this item - a film can have several - with the
        subtitles Plex holds for each: [{"path", "subtitles": [(label, url)]}].

        Only subtitles Plex keeps outside the file are listed (downloaded ones,
        or a sidecar Plex found); those inside a file are read from the file.
        """
        items = self._get(f"/library/metadata/{item_id}").get("Metadata") or []
        out = []
        for media in (items[0].get("Media") or []) if items else []:
            for part in media.get("Part") or []:
                subs = []
                for st in part.get("Stream") or []:
                    if (int(st.get("streamType") or 0) != 3 or not st.get("key")
                            or _forced(st.get("forced"), st.get("title"))
                            or not _english(st.get("languageCode") or st.get("language"))
                            or str(st.get("codec", "")).lower() not in _TEXT_CODECS):
                        continue
                    subs.append((st.get("displayTitle") or "Plex subtitles",
                                 f"{self.url.rstrip('/')}{st['key']}"))
                if part.get("file"):
                    out.append({"path": part["file"], "subtitles": subs})
        return out

    def download(self, url: str) -> str:
        resp = httpx.get(url, params={"X-Plex-Token": self.token}, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text

    def find(self, kind: str, path: str, title: str = "") -> str:
        """The item id of the film or episode whose file is `path`, or "".

        An episode is found through its show, by name, and then matched on the
        file itself - so a show with the same name elsewhere cannot answer.
        """
        if kind == "movie":
            return _pick(self.movies(), path)
        sections = self._sections("show") if title else []
        for name in title_variants(title):
            for section in sections:
                for show in self._all(section, title=name):
                    found = _pick(self.episodes(str(show.get("ratingKey"))), path)
                    if found:
                        return found
        return ""

    def search_subtitles(self, item_id: str) -> str:
        """Ask Plex to find English subtitles online and attach the best match,
        as its own "Search subtitles" does. The label of what was attached, or ""
        when Plex found nothing.

        Plex searches with its own OpenSubtitles account and matches on the
        file, so a hit is usually timed for this very copy.
        """
        found = self._get(f"/library/metadata/{item_id}/subtitles",
                          language="en", hearingImpaired=0, forced=0, timeout=SEARCH_TIMEOUT)
        results = [s for s in (found.get("Stream") or [])
                   if s.get("key") and not _forced(s.get("forced"), s.get("title"))]
        if not results:
            return ""
        best = max(results, key=lambda s: (s.get("perfectMatch") in (True, 1, "1"),
                                           float(s.get("score") or 0)))
        self._send("PUT", f"/library/metadata/{item_id}/subtitles", key=best["key"])
        return str(best.get("displayTitle") or best.get("title") or "English")

    def _send(self, method: str, path: str, **params) -> None:
        params["X-Plex-Token"] = self.token
        try:
            resp = httpx.request(method, f"{self.url.rstrip('/')}{path}", params=params,
                                 timeout=SEARCH_TIMEOUT)
        except httpx.HTTPError as exc:
            raise LibraryError(f"could not reach Plex: {exc}") from exc
        if resp.status_code >= 400:
            raise LibraryError(f"Plex said {resp.status_code}")

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

    def _get(self, path: str, timeout: float = 0, **params):
        if not self.url or not self.api_key:
            raise LibraryError("Jellyfin address and API key are not set")
        try:
            resp = httpx.get(f"{self.url.rstrip('/')}{path}", params=params,
                             headers={**jellyfin_headers(self.api_key),
                                      "Accept": "application/json"},
                             timeout=timeout or TIMEOUT)
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

    def versions(self, item_id: str) -> list[dict]:
        """As PlexLibrary.versions. Jellyfin lists a film's other versions as
        further MediaSources of the same item, and converts any text subtitle
        it holds to SRT on request."""
        body = self._get("/Items", Ids=item_id, Fields="Path,MediaSources")
        items = body.get("Items") or []
        out = []
        for source in (items[0].get("MediaSources") or []) if items else []:
            subs = []
            for st in source.get("MediaStreams") or []:
                if (st.get("Type") != "Subtitle" or not st.get("IsExternal")
                        or not st.get("IsTextSubtitleStream")
                        or _forced(st.get("IsForced"), st.get("Title"))
                        or not _english(st.get("Language"))):
                    continue
                subs.append((st.get("DisplayTitle") or "Jellyfin subtitles",
                             f"{self.url.rstrip('/')}/Videos/{item_id}/{source.get('Id')}"
                             f"/Subtitles/{st.get('Index')}/Stream.srt"))
            if source.get("Path"):
                out.append({"path": source["Path"], "subtitles": subs})
        return out

    def download(self, url: str) -> str:
        resp = httpx.get(url, headers=jellyfin_headers(self.api_key), timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text

    def find(self, kind: str, path: str, title: str = "") -> str:
        """As PlexLibrary.find."""
        if kind == "movie":
            return _pick(self.movies(), path)
        for name in title_variants(title):
            for show in self._items(IncludeItemTypes="Series", SearchTerm=name, Fields="Path"):
                found = _pick(self.episodes(str(show.get("Id"))), path)
                if found:
                    return found
        return ""

    def search_subtitles(self, item_id: str) -> str:
        """As PlexLibrary.search_subtitles. Needs a subtitle provider plugin
        (OpenSubtitles) installed in Jellyfin; without one the search is empty."""
        found = self._get(f"/Items/{item_id}/RemoteSearch/Subtitles/eng",
                          timeout=SEARCH_TIMEOUT) or []
        results = [r for r in found if r.get("Id") and not r.get("IsForced")]
        if not results:
            return ""
        best = max(results, key=lambda r: (bool(r.get("IsHashMatch")),
                                           float(r.get("CommunityRating") or 0),
                                           int(r.get("DownloadCount") or 0)))
        try:
            resp = httpx.post(f"{self.url.rstrip('/')}/Items/{item_id}/RemoteSearch/"
                              f"Subtitles/{best['Id']}",
                              headers=jellyfin_headers(self.api_key), timeout=SEARCH_TIMEOUT)
        except httpx.HTTPError as exc:
            raise LibraryError(f"could not reach Jellyfin: {exc}") from exc
        if resp.status_code >= 400:
            raise LibraryError(f"Jellyfin said {resp.status_code}")
        return str(best.get("Name") or "English")

    def test(self) -> dict:
        info = self._get("/System/Info")
        return {"ok": True, "app": info.get("ServerName") or "Jellyfin",
                "version": info.get("Version", "")}


# ---------------------------------------------------------------------------
#  Sonarr for shows, the media server for films
# ---------------------------------------------------------------------------

@dataclass
class SonarrLibrary:
    """Sonarr can say what has not aired yet, which neither media server can,
    so this is the only source that can fill the Upcoming calendar. Films come
    from the media server either way."""

    sonarr: object
    films: object          # a PlexLibrary / JellyfinLibrary, or None

    name = "sonarr"
    has_calendar = True

    def shows(self) -> list[dict]:
        return self.sonarr.series()

    def episodes(self, show_id: str) -> list[dict]:
        return self.sonarr.episodes(int(show_id))

    def movies(self) -> list[dict]:
        if self.films is None:
            raise LibraryError("they come from your media server - choose Plex "
                               "or Jellyfin under Media server")
        return self.films.movies()

    def recent_episodes(self, limit: int = 12) -> list[dict]:
        return self.sonarr.recent_imports(limit)

    def roots(self) -> list[dict]:
        """TV folders as Sonarr sees them, film folders as the media server does."""
        out = [{"library": "Sonarr", "kind": "show", "path": r["path"]}
               for r in (getattr(self.sonarr, "root_folders", lambda: [])() or [])
               if r.get("path")]
        if self.films is not None:
            out += [r for r in self.films.roots() if r.get("kind") == "movie"]
        return out


PlexLibrary.has_calendar = False
JellyfinLibrary.has_calendar = False


def source_name(settings) -> str:
    """sonarr | plex | jellyfin. "arr" was Sonarr + Radarr, which is now Sonarr."""
    source = getattr(settings, "library_source", "sonarr")
    return source if source in ("plex", "jellyfin") else "sonarr"


def server(settings, name: str = ""):
    """The Plex or Jellyfin client by name - the media server's by default."""
    name = name or getattr(settings, "media_server", "none")
    if name == "plex":
        return PlexLibrary(settings.plex_url, settings.plex_token)
    if name == "jellyfin":
        return JellyfinLibrary(settings.jellyfin_url, settings.jellyfin_api_key)
    return None


def film_source(settings) -> str:
    """Which media server the films come from, or "" when there is none."""
    source = source_name(settings)
    if source in ("plex", "jellyfin"):
        return source
    chosen = getattr(settings, "media_server", "none")
    return chosen if chosen in ("plex", "jellyfin") else ""


def build(settings):
    """Whichever source the settings name."""
    source = source_name(settings)
    if source in ("plex", "jellyfin"):
        return server(settings, source)
    from . import arr as _arr
    films = film_source(settings)
    return SonarrLibrary(_arr.Sonarr(settings.sonarr.url, settings.sonarr.api_key),
                         server(settings, films) if films else None)
