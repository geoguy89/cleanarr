# Cleanarr

Cleanarr is an \*arr-inspired, *vibe-coded* application that mutes profanity in
media you already own.

It adds a second audio track, **“Cleaned - English”** by default, with the swearing
silenced. The original is untouched and stays the default — in Plex or Jellyfin
you pick the clean one from the audio menu. **One file, two tracks, not two
copies.**

Built for a household with a small child in the room, so when anything is
uncertain the word gets muted.

```
 your library ──▶ queue ──▶ ffmpeg ──▶ Whisper ──▶ word list
                                                       │
                         file with a new track ◀── mute + remux
```

---

## What it looks like

**Home** — what landed lately, and what it has cost so far.

![Home](docs/images/home.jpg)

**Your library**, with badges for cleaned, queued, auto-cleaning, or nothing on
disk yet.

![Shows](docs/images/shows.jpg)

**Inside a show** — clean one episode, a season, everything outstanding, or
from here on. Removing a cleaned track is just as easy.

![Episodes](docs/images/episodes.jpg)

**Every mute is on the record**, so a wrong call can be found rather than
guessed at.

![Job details](docs/images/details.jpg)

**Cleaned** — searchable history, and what the extra tracks cost in disk.

![Cleaned](docs/images/cleaned.jpg)

**Upcoming** — Sonarr's calendar without leaving the app. Sonarr only; the tab
hides itself otherwise.

![Upcoming](docs/images/upcoming.jpg)

---

## Requirements

| | |
|---|---|
| **A library** | Sonarr + Radarr, **or** Plex, **or** Jellyfin. Read-only — Cleanarr never writes to any of them. |
| **Media** | Reachable from this container. The paths do not have to match your library's; see [Paths](#paths). |
| **GPU** | Optional. NVIDIA is much faster; CPU works. See [Hardware](#hardware). |
| **Disk** | ~1.5 GB for the speech model, plus ~90 MB per cleaned 25-minute episode. |
| **Ollama** | Optional, off by default. See [The second opinion](#the-second-opinion). |

---

## Installing

### Docker Compose

```yaml
services:
  cleanarr:
    image: ghcr.io/geoguy89/cleanarr:latest
    container_name: cleanarr
    restart: unless-stopped
    stop_grace_period: 45s
    ports:
      - "8477:8477"
    volumes:
      - /path/to/appdata/cleanarr:/config
      # The same path on BOTH sides: Cleanarr opens the paths your library
      # reports, so they have to mean the same thing in here.
      - /mnt/user/data:/mnt/user/data
    environment:
      - TZ=America/New_York
```

```bash
docker compose up -d
```

**With an NVIDIA GPU**, add this to the service as well:

```yaml
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

> Add it **only** if you have one. Docker will not start a container whose
> device reservation cannot be met, so on a machine with no NVIDIA card these
> lines stop Cleanarr running at all, with an error that says nothing about
> Cleanarr.

> Also on Docker Hub as `geoguy89/cleanarr:latest`. GHCR is the better
> default — Docker Hub rate-limits anonymous pulls.

### docker run

```bash
# The media volume is the same path on both sides: Cleanarr opens the paths
# your library reports, so they have to mean the same thing in the container.
docker run -d \
  --name cleanarr \
  --restart unless-stopped \
  -e TZ=America/New_York \
  -p 8477:8477 \
  -v /path/to/appdata/cleanarr:/config \
  -v /mnt/user/data:/mnt/user/data \
  ghcr.io/geoguy89/cleanarr:latest
```

With an NVIDIA GPU, add `--runtime nvidia -e NVIDIA_VISIBLE_DEVICES=all -e
NVIDIA_DRIVER_CAPABILITIES=compute,utility`.

### Unraid

Copy `docker/my-Cleanarr.xml` to
`/boot/config/plugins/dockerMan/templates-user/`, then **Docker → Add
Container** and pick *cleanarr* from the template dropdown. That gives you the
icon, a WebUI link and a working Edit dialog. Updates work normally — the
template pulls from ghcr.io.

### Building it yourself

The published image is `linux/amd64` only; its CUDA base has no arm64 build.

```bash
git clone https://github.com/geoguy89/cleanarr.git
cd cleanarr/docker && docker compose up -d --build
```

### Hardware

Listening is the only demanding part.

| | Speed |
|---|---|
| **NVIDIA GPU** | ~85 s for a 25-minute episode |
| **CPU only** | Several times slower. Queue it overnight. |
| **AMD GPU** | Runs, but on the CPU — see below |

Cleanarr picks GPU or CPU on its own. **Settings → Listening** shows which it
found and lets you force either.

faster-whisper runs on CTranslate2, which has CPU and CUDA backends and **no
ROCm**, so an AMD card sits idle. To use one, run something that supports it —
[whisper.cpp](https://github.com/ggml-org/whisper.cpp) has Vulkan and ROCm
backends — and point Cleanarr at it under **Settings → Listening → Use my own
Whisper server**. Anything exposing the OpenAI transcription API works.

> A remote server must return **word-level** timestamps. Cleanarr mutes half a
> second around one word; with only line-level timings it would have to blank
> four seconds of dialogue to catch one syllable. The **Test** button checks
> for this.

---

## First run

Open `http://<host>:8477` and work down **Settings**; it is numbered in the
order things need doing.

1. **Your library** — Sonarr + Radarr, Plex or Jellyfin, with a test button.
2. **What gets silenced** — four switchable lists, plus your own always/never
   words.
3. **Listening** — download the speech model here rather than during your first
   clean.
4. **Muting** — padding and fade. The defaults are fine.
5. **Second opinion** — optional, off by default.
6. **Media server** — optional, for refreshing and pausing during transcodes.
7. **Who can use this** — set a password if this is reachable from outside your
   network.

Then clean one episode before turning it loose on a season.

### Where the library comes from

Cleanarr needs one thing: what you own, and where each file is.

| Source | Needs | Notes |
|---|---|---|
| **Sonarr + Radarr** | both, with API keys | Knows the most, and the only one that can fill the Upcoming calendar |
| **Plex** | address + token | No \*arr apps needed. Artwork comes from Plex too. |
| **Jellyfin** | address + API key | Same again |

Everything else works the same whichever you pick, including cleaning new
episodes automatically. The Plex and Jellyfin credentials are the same ones
used for refreshing and transcode pauses — there is no second set to keep in
step.

### Paths

Your library reports where each file is **as it sees it**, and Cleanarr opens
that exact path. So mount your media into this container **at the same paths
your library reports**:

```yaml
volumes:
  - /mnt/user/data:/mnt/user/data    # whatever Plex, Sonarr or Radarr calls it
```

The two often do not agree — a Plex server on another machine, or one reaching
storage over its own mount, reports paths that mean nothing here. The symptom
is the library browsing perfectly while every job fails with *“not found from
this container”*.

**Settings → Your library** shows a check for each library folder: what it
reports and whether Cleanarr can open it. When it cannot, it also says where
that folder looks to be mounted instead, so the fix is usually one line of the
compose file.

There is no path-rewriting setting, deliberately. The media has to be mounted
into this container either way — NFS or SMB from another machine is fine, it
just has to be mounted — and once it is being mounted, mounting it at the path
the library reports costs nothing and leaves one fewer thing to get wrong.

---

## How it works

1. **Probe** — ffprobe reads the file. One already carrying a cleaned track is
   skipped unless you ask again.
2. **Extract** — the English audio, as 16 kHz mono.
3. **Listen** — Whisper transcribes with **word-level** timestamps. A subtitle
   line says a swear happened somewhere in four seconds; words give ~50 ms.
4. **Match** — whole words only, so *class* and *cockpit* are never caught. A
   phrase mutes only the profane word: *“oh my god”* silences *god*.
5. **Mute** — each match silenced with a 20 ms fade at each edge.
6. **Verify, then swap** — the new file replaces the original **only** after
   ffprobe confirms every original stream is present plus one, at the same
   duration, correctly interleaved.

Transcripts are cached against the audio, so changing a word list and
re-cleaning skips the slow part.

---

## The second opinion

**Optional, off by default, and most people should leave it off.**

Speech recognition writes homophones. A scene about drywall produced nine mutes
of *“cock”* — the word was *“caulk”*. No word list fixes that, because the
transcript genuinely contains the rude one.

So a word you list is sent to **your own Ollama**, with the sentence around it,
and is left in only if the model names the ordinary word it heard instead.
Nothing leaves your network; a blank address skips the step.

Across 196 checks on a real library it changed the outcome **twice**, at
30–100 s each — hence the default. Anything unanswered stays muted.

---

## Shows that clean themselves

Marking a show cleans **episodes downloaded from now on**. Nothing already on
disk is touched; catching up is a separate button. A show with no files yet can
be marked, so a new series arrives clean.

Checked every 10 minutes. For instant cleaning, point a Sonarr **Connect →
Webhook** (*On Import*) at `http://<host>:8477/api/webhook/sonarr`.

---

## Things worth knowing

* **medium.en is the only model offered.** large-v3 took 50% longer and caught
  *fewer* swears — it favours tidy prose, and tidy prose smooths swearing away.
* **Whisper's silence filter is off.** On one 43-minute episode it skipped 19
  real profanities and saved no measurable time.
* **Nothing is written until it verifies.** An early build produced a file Plex
  hung on, because the cleaned audio landed 485 MB from the picture inside the
  container. The interleave check exists because of that episode.
* **One job at a time**, on purpose — the GPU is shared.

---

## Development

```
server/cleanarr/
  words.py      word lists and whole-word matching     (tools/test_words.py)
  judge.py      the second opinion                     (tools/test_judge.py)
  asr.py        Whisper, local or remote, with caching
  media.py      ffprobe/ffmpeg: probe, mute, remux, verify, swap
  library.py    Sonarr/Radarr, Plex or Jellyfin as the library
  pipeline.py   one file, start to finish
  worker.py     the queue
  arr.py        Sonarr, Radarr, Plex and Jellyfin clients
  auth.py       optional login
  main.py       API and static page
web/            the page
```

Tests are plain scripts, no framework:

```bash
python tools/test_words.py
python tools/test_judge.py
```

---

## Licence

MIT — see [LICENSE](LICENSE).
